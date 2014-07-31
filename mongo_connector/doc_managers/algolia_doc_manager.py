# Copyright 2014 Algolia
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Receives documents from the oplog worker threads and indexes them
    into the backend.

    This file is a document manager for the Algolia search engine.
    """
import logging
import re
import json
from datetime import datetime
import bson.json_util as bsjson
import bson
import copy

from algoliasearch import algoliasearch
from mongo_connector import errors
from mongo_connector.doc_managers import DocManagerBase
from threading import Timer, RLock

decoder = json.JSONDecoder()


def clean_path(dirty):
    # python dictionary subscript style:
    if re.match(r'^\[', dirty):
        return re.split(r'\'\]\[\'', re.sub(r'^\[\'|\'\]$', '', dirty))
    # mongo op dot-notation style:
    return dirty.split('.')

def get_at(doc, path, create_anyway = False):
    node = doc
    last = len(path) - 1
    if last == 0:
        return doc.get(path[0])
    for index, edge in enumerate(path):
        if edge in node:
            node = node[edge]
        elif index == last:
            return None
        elif create_anyway:
            node = node[edge] = {}
        else:
            return None
    return node

def set_at(doc, path, value):
    node = get_at(doc, path[:-1], create_anyway = True)
    node[path[-1]] = value

def set_or_append(doc, path, value, append = False):
    if append:
        get_at(doc, path).append(value)
    else:
        set_at(doc, path, value)

def unix_time(dt = datetime.now()):
    epoch = datetime.utcfromtimestamp(0)
    delta = dt - epoch
    return delta.total_seconds()

def unix_time_millis(dt = datetime.now()):
    return int(round(unix_time(dt) * 1000.0))

def filter_value(value, filter):
    if filter == "":
        return True
    try:
        return eval(re.sub(r'\$_', 'value', filter))
    except Exception as e:
        logging.warn("Error raised from expression: {filter} with value {value}".format(**locals()))
        logging.warn(e)
        # it should return false to prevent potentially sensitive data from being synced
        return False

class DocManager(DocManagerBase):
    """The DocManager class creates a connection to the Algolia engine and
        adds/removes documents, and in the case of rollback, searches for them.

        Algolia's native 'objectID' field is used to store the unique_key.
        """

    BATCH_SIZE = 1000
    AUTO_COMMIT_DELAY_S = 10

    def __init__(self, url, unique_key='_id', **kwargs):
        """ Establish a connection to Algolia using target url 'APPLICATION_ID:API_KEY:INDEX_NAME'
        """
        application_id, api_key, index = url.split(':')
        self.algolia = algoliasearch.Client(application_id, api_key)
        self.index = self.algolia.initIndex(index)
        self.unique_key = unique_key
        self.last_object_id = None
        self.batch = []
        self.mutex = RLock()
        self.auto_commit = True
        self.run_auto_commit()
        try:
            json = open("algolia_fields_" + index + ".json", 'r')
            self.attributes_filter = decoder.decode(json.read())
            logging.info("Algolia Connector: Start with filter.")
        except IOError: # No filter file
            self.attributes_filter = None
            logging.info("Algolia Connector: Start without filter.")
        try:
            json = open("algolia_remap_" + index + ".json", 'r')
            self.attributes_remap = decoder.decode(json.read())
            logging.info("Algolia Connector: Start with remapper.")
        except IOError: # No filter file
            self.attributes_remap = None
            logging.info("Algolia Connector: Start without remapper.")
        try:
            f = open("algolia_postproc_" + index, 'r')
            self.postproc = f.read()
            logging.info("Algolia Connector: Start with post processing.")
        except IOError: # No filter file
            self.postproc = None
            logging.info("Algolia Connector: Start without post processing.")


    def stop(self):
        """ Stops the instance
        """
        self.auto_commit = False

    def remap(self, tree):
        if self.attributes_remap is None or not tree in self.attributes_remap:
            return tree
        return  self.attributes_remap[tree]

    def serialize(self, value):
        if isinstance(value, bson.objectid.ObjectId):
            return str(value)
        else:
            return value

    def apply_filter(self, doc, filter):
        if not filter:
            # alway return a new object:
            return (copy.deepcopy(doc), True)

        filtered_doc = {}
        all_or_nothing = '*all*' in filter

        for raw_key, expr in filter.iteritems():
            if raw_key == '*all*':
                continue
            key = clean_path(raw_key)
            values = get_at(doc, key)
            if type(values) == list:
                set_at(filtered_doc, key, [])
                append = True
            else:
                append = False
                values = [values]

            state = True

            for value in values:
                if isinstance(value, dict):
                    part, partState = self.apply_filter(value, filter[raw_key])
                    if partState:
                        set_or_append(filtered_doc, key, self.serialize(part), append)
                    elif all_or_nothing:
                        node = get_at(filtered_doc, key[:-1])
                        del node[key[-1]]
                        return filtered_doc, False
                else:
                    if filter_value(value, filter[raw_key]):
                        set_or_append(filtered_doc, key, self.serialize(value), append)
                    elif all_or_nothing:
                        return filtered_doc, False
                    else:
                        state = False

        return (filtered_doc, state)

    def apply_remap(self, doc):
        if not self.attributes_remap:
            return doc
        remapped_doc = {}
        for raw_source_key, raw_target_key in self.attributes_remap.items():
            # clean the keys, making a list from possible notations:
            source_key = clean_path(raw_source_key)
            target_key = clean_path(raw_target_key)

            if target_key is '*skip*':
                continue

            # get the value from the source doc:
            value = get_at(doc, source_key)

            # special case for "_ts" field:
            if source_key == ['_ts'] and target_key == ["*ts*"]:
                value = value if value else str(unix_time_millis())

            set_at(remapped_doc, target_key, value)
        return remapped_doc

    def update(self, doc, update_spec):
        self.upsert(self.apply_update(doc, update_spec))

    def upsert(self, doc):
        """ Update or insert a document into Algolia
        """
        with self.mutex:
            self.last_object_id = str(doc[self.unique_key]) # mongodb ObjectID is not serializable
            remapped_doc = self.apply_remap(doc)
            remapped_doc['objectID'] = self.last_object_id

            filtered_doc, state = self.apply_filter(remapped_doc, self.attributes_filter)
            if not state: # delete in case of update
                self.batch.append({ 'action': 'deleteObject', 'body': {'objectID': self.last_object_id } })
                return

            if self.postproc is not None:
                exec(re.sub(r"_\$", "doc", self.postproc))
            self.batch.append({ 'action': 'addObject', 'body': filtered_doc })
            if len(self.batch) >= DocManager.BATCH_SIZE:
                self.commit()

    def remove(self, doc):
        """ Removes documents from Algolia
        """
        with self.mutex:
            self.batch.append({ 'action': 'deleteObject', 'body': {"objectID" : str(doc[self.unique_key])} })
            if len(self.batch) >= DocManager.BATCH_SIZE:
                self.commit()

    def search(self, start_ts, end_ts):
        """ Called to query Algolia for documents in a time range.
        """
        try:
            params = {
                numericFilters: '_ts>=%d,_ts<=%d' % (start_ts, end_ts),
                exhaustive: True,
                hitsPerPage: 100000000
            }
            return self.index.search('', params)['hits']
        except algoliasearch.AlgoliaException as e:
            raise errors.ConnectionFailed("Could not connect to Algolia Search: %s" % e)

    def commit(self):
        """ Send the current batch of updates
        """
        try:
            request = {}
            with self.mutex:
                if len(self.batch) == 0:
                    return
                self.index.batch({ 'requests': self.batch })
                self.index.setSettings({ 'userData': { 'lastObjectID': self.last_object_id } })
                self.batch = []
        except algoliasearch.AlgoliaException as e:
            raise errors.ConnectionFailed("Could not connect to Algolia Search: %s" % e)

    def run_auto_commit(self):
        """ Periodically commits to Algolia.
        """
        self.commit()
        if self.auto_commit:
            Timer(DocManager.AUTO_COMMIT_DELAY_S, self.run_auto_commit).start()

    def get_last_doc(self):
        """ Returns the last document stored in Algolia.
        """
        last_object_id = self.get_last_object_id()
        if last_object_id is None:
            return None
        try:
            return self.index.getObject(last_object_id)
        except algoliasearch.AlgoliaException as e:
            raise errors.ConnectionFailed("Could not connect to Algolia Search: %s" % e)

    def get_last_object_id(self):
        try:
            return (self.index.getSettings().get('userData', {})).get('lastObjectID', None)
        except algoliasearch.AlgoliaException as e:
            raise errors.ConnectionFailed("Could not connect to Algolia Search: %s" % e)
