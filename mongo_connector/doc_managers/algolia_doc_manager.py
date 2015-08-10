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
import base64
import logging
import json
import re
import copy
from datetime import datetime

from threading import Timer, RLock

import bson.json_util
import bson.json_util as bsjson

from algoliasearch import algoliasearch
import urllib3

from mongo_connector import errors
from mongo_connector.compat import u
from mongo_connector.constants import (DEFAULT_COMMIT_INTERVAL,
                                       DEFAULT_MAX_BULK)
from mongo_connector.util import exception_wrapper, retry_until_ok
from mongo_connector.doc_managers.doc_manager_base import DocManagerBase
from mongo_connector.doc_managers.formatters import DefaultDocumentFormatter

decoder = json.JSONDecoder()


def clean_path(dirty):
    """Convert a string of python subscript notation or mongo dot-notation to a
        list of strings.
        """
    # handle python dictionary subscription style, e.g. `"['key1']['key2']"`:
    if re.match(r'^\[', dirty):
        return re.split(r'\'\]\[\'', re.sub(r'^\[\'|\'\]$', '', dirty))
    # handle mongo op dot-notation style, e.g. `"key1.key2"`:
    return dirty.split('.')


def get_at(doc, path, create_anyway=False):
    """Get the value, if any, of the document at the given path, optionally
        mutating the document to create nested dictionaries as necessary.
        """
    node = doc
    last = len(path) - 1
    if last == 0:
        return doc.get(path[0])
    for index, edge in enumerate(path):
        if edge in node:
            node = node[edge]
        elif index == last or not create_anyway:
            # the key doesn't exist, and this is the end of the path:
            return None
        else:
            # create anyway will create any missing nodes:
            node = node[edge] = {}
    return node


def set_at(doc, path, value):
    """Set the value of the document at the given path."""
    node = get_at(doc, path[:-1], create_anyway=True)
    node[path[-1]] = value


def put_at(doc, path, value, append=False):
    """Set or append the given value to the document at the given path"""
    if append:
        get_at(doc, path).append(value)
    else:
        set_at(doc, path, value)


def unix_time(dt=datetime.now()):
    epoch = datetime.utcfromtimestamp(0)
    delta = dt - epoch
    return delta.total_seconds()


def unix_time_millis(dt=datetime.now()):
    return int(round(unix_time(dt) * 1000.0))


def serialize(value):
    """If the value is an BSON ObjectId, cast it to a string."""
    if isinstance(value, bson.objectid.ObjectId):
        return str(value)
    else:
        return value


def filter_value(value, expr):
    """Evaluate the given expression in the context of the given value."""
    if expr == "":
        return True
    try:
        return eval(re.sub(r'\$_', 'value', expr))
    except Exception as e:
        logging.warn("""
            Error raised from expression: {expr} with value {value}
            """.format(**locals()))
        logging.warn(e)
        # return false to prevent potentially sensitive data from being synced:
        return False


class DocManager(DocManagerBase):
    """The DocManager class creates a connection to the Algolia engine and
        adds/removes documents, and in the case of rollback, searches for them.

        Algolia's native 'objectID' field is used to store the unique_key.
        """

    def __init__(self, url,
        unique_key='_id',
        auto_commit_interval=10,
        chunk_size=1000,
        commit_sync=False,
        commit_waittask_interval=1,
        **kwargs):
        """Establish a connection to Algolia using target url
            'APPLICATION_ID:API_KEY:INDEX_NAME'
        """
        application_id, api_key, index = url.split(':')
        self.algolia = algoliasearch.Client(application_id, api_key)
        self.index = self.algolia.initIndex(index)
        logging.info("Algolia Connector: APP is " + str(application_id))
        logging.info("Algolia Connector: INDEX is " + str(index))

        self.unique_key = unique_key
        self.last_object_id = None
        self.batch = []
        self.mutex = RLock()

        self.auto_commit_interval = auto_commit_interval
        self.chunk_size = chunk_size
        self.commit_sync = commit_sync
        self.commit_waittask_interval = commit_waittask_interval
        if self.auto_commit_interval not in [None, 0]:
            logging.info("Algolia Connector: AUTO_COMMIT_INTERVAL every " + str(self.auto_commit_interval) + " second(s)")
            logging.info("Algolia Connector: CHUNK_SIZE is " + str(self.chunk_size))
            logging.info("Algilia Connector: COMMIT_WAITTASK_INTERVAL is " + str(self.commit_waittask_interval) + " second(s)")
            self.run_auto_commit()

        try:
            json = open("algolia_fields_" + index + ".json", 'r')
            self.attributes_filter = decoder.decode(json.read())
            logging.info("Algolia Connector: Start with filter.")
        except IOError:  # No "fields" filter file
            self.attributes_filter = None
            logging.info("Algolia Connector: Start without filter.")
        try:
            json = open("algolia_remap_" + index + ".json", 'r')
            self.attributes_remap = decoder.decode(json.read())
            logging.info("Algolia Connector: Start with remapper.")
        except IOError:  # No "remap" filter file
            self.attributes_remap = None
            logging.info("Algolia Connector: Start without remapper.")
        try:
            f = open("algolia_postproc_" + index + ".py", 'r')
            self.postproc = f.read()
            logging.info("Algolia Connector: Start with post processing.")
        except IOError:  # No "postproc" filter file
            self.postproc = None
            logging.info("Algolia Connector: Start without post processing.")

    def stop(self):
        """ Stops the instance
        """
        self.auto_commit = False

    def apply_remap(self, doc):
        """Copy the values of user-defined fields from the source document to
            user-defined fields in a new target document, then return the
            targetdocument.
            """
        if not self.attributes_remap:
            return doc
        remapped_doc = {}
        for raw_source_key, raw_target_key in self.attributes_remap.items():
            # clean the keys, making a list from possible notations:
            source_key = clean_path(raw_source_key)
            target_key = clean_path(raw_target_key)

            # get the value from the source doc:
            value = get_at(doc, source_key)

            # special case for "_ts" field:
            if source_key == ['_ts'] and target_key == ["*ts*"]:
                value = value if value else str(unix_time_millis())

            set_at(remapped_doc, target_key, value)
        return remapped_doc

    def apply_filter(self, doc, filter):
        """Recursively copy the values of user-defined fields from the source
            document to a new target document by testing each value against a
            corresponding user-defined expression. If the expression returns
            true for a given value, copy that value to the corresponding field
            in the target document. If the special `*all*` filter is used for
            a given document and an adjacent field's expression returns false
            for a given value, remove the document containing that field from
            its parent in the tree of the target document.
            """
        if not filter:
            # alway return a new object:
            return (copy.deepcopy(doc), True)
        filtered_doc = {}
        all_or_nothing = '*all*' in filter
        try:
            items = filter.iteritems()
        except AttributeError:
            items = filter.items();
        for raw_key, expr in items:
            if raw_key == '*all*':
                continue
            key = clean_path(raw_key)
            values = get_at(doc, key)
            state = True
            if type(values) == list:
                append = True
                set_at(filtered_doc, key, [])
            else:
                append = False
                values = [values]
            for value in values:
                if isinstance(value, dict):
                    sub, sub_state = self.apply_filter(value, filter[raw_key])
                    if sub_state:
                        put_at(filtered_doc, key, serialize(sub), append)
                    elif all_or_nothing:
                        node = get_at(filtered_doc, key[:-1])
                        del node[key[-1]]
                        return filtered_doc, False
                elif filter_value(value, filter[raw_key]):
                    put_at(filtered_doc, key, serialize(value), append)
                elif all_or_nothing:
                    return filtered_doc, False
                else:
                    state = False
        return (filtered_doc, state)

    def update_can_be_ignored(self, update_spec):

        if not self.attributes_filter:
            return False

        if "$set" not in update_spec and "$unset" not in update_spec:
            return False

        def attr_can_be_ignored(attr):
            path = clean_path(re.sub(r'\.\d+\.', '.', attr))
            if get_at(self.attributes_filter, path, False) is not None:
                return False
            return True

        if "$set" in update_spec:
            specAttrs = self.apply_remap(update_spec["$set"])
            for attr in specAttrs:
                if not attr_can_be_ignored(attr):
                    logging.debug("Algolia Connector: Update needed ($set " + attr + ")")
                    return False
        if "$unset" in update_spec:
            specAttrs = self.apply_remap(update_spec["$unset"])
            for attr in specAttrs:
                if not attr_can_be_ignored(attr):
                    logging.debug("Algolia Connector: Update needed ($unset " + attr + ")")
                    return False

        logging.debug("Algolia Connector: Update can be ignored")
        return True

    def _db_and_collection(self, namespace):
        return namespace.split('.', 1)

    def get_source_doc(self, document_id, namespace):
        db, coll = self._db_and_collection(namespace)
        if not hasattr(self, 'connector_mongo_client'):
            raise Exception('connector mongo client not found')
        doc = self.connector_mongo_client[db][coll].find(
            {'_id': document_id},
        )[0]
        return doc

    def convert_update_spec_to_partial_doc(self, update_spec):
        if "$set" in update_spec:
            specAttrs = update_spec["$set"]
            for attr in specAttrs:
                set_at(update_spec, attr, specAttrs.get(attr))
            del update_spec["$set"]
        if "$unset" in update_spec:
            specAttrs = update_spec["$unset"]
            for attr in specAttrs:
                set_at(update_spec, attr, None)
            del update_spec["$unset"]


    def update(self, document_id, update_spec, namespace = None, timestamp = None):

        if self.update_can_be_ignored(update_spec):
            logging.info("Algolia Connector: Update Skipped (no changes passed the filter)")
            return

        if "$set" not in update_spec and "$unset" not in update_spec:
            logging.info("Algolia Connector: Document full replace")
            self.upsert(update_spec, False, namespace, timestamp)
        elif (self.postproc is not None):
            # if postproc is used, mongo update_spec cannot be applied to a processed doc
            # the full source document from mongo is needed
            logging.info("Algolia Connector: Reprocessing source document")
            doc = self.get_source_doc(document_id, namespace)
            self.upsert(doc, False, namespace, timestamp)
        else:
            # convert mongo update_spec to algolia partial update body
            logging.info("Algolia Connector: Partial update")
            partial_doc = convert_update_spec_to_partial_doc(update_spec)
            self.upsert(partial_doc, True, namespace, timestamp)

    def upsert(self, doc, update = False, namespace = None, timestamp = None):
        """ Update or insert a document into Algolia
        """
        with self.mutex:
            self.last_object_id = serialize(doc.get(self.unique_key) or doc['objectID'])
            filtered_doc, state = self.apply_filter(self.apply_remap(doc),
                                                    self.attributes_filter)
            filtered_doc['objectID'] = self.last_object_id

            #if not state:  # delete in case of update
            #    self.batch.append({'action': 'deleteObject',
            #                       'body': {'objectID': last_object_id}})
            #    return

            if self.postproc is not None:
                exec(re.sub(r"_\$", "filtered_doc", self.postproc))

            self.batch.append({'action': 'partialUpdateObject' if update else 'addObject', 'body': filtered_doc})
            if len(self.batch) >= self.chunk_size:
                self.commit()

    def remove(self, document_id, namespace = None, timestamp = None):
        """ Removes documents from Algolia
        """
        with self.mutex:
            self.batch.append(
                {'action': 'deleteObject',
                 'body': {'objectID': str(document_id)}})
            if len(self.batch) >= self.chunk_size:
                self.commit()

    def search(self, start_ts, end_ts):
        """ Called to query Algolia for documents in a time range.
        """
        try:
            params = {
                'numericFilters': '_ts>=%d,_ts<=%d' % (start_ts, end_ts),
                'exhaustive': True,
                'hitsPerPage': 100000000
            }
            return self.index.search('', params)['hits']
        except algoliasearch.AlgoliaException as e:
            raise errors.ConnectionFailed(
                "Could not connect to Algolia Search: %s" % e)

    def commit(self):
        """ Send the current batch of updates
        """
        try:
            request = {}
            with self.mutex:
                if len(self.batch) == 0:
                    return
                self.index.batch({'requests': self.batch})
                res = self.index.setSettings({'userData': {'lastObjectID': self.last_object_id}})
                logging.debug("Algolia Connector: commited with taskID " + str(res['taskID']))
                self.batch = []
                if self.commit_sync:
                    self.index.waitTask(res['taskID'], self.commit_waittask_interval * 1000)
        except (algoliasearch.AlgoliaException, urllib3.exceptions.MaxRetryError) as e:
            raise errors.OperationFailed(
                "Could not connect to Algolia Search: %s" % e)

    def run_auto_commit(self):
        """ Periodically commits to Algolia.
        """
        try:
            self.commit()
        except Exception as e:
            logging.warning(e)
        if self.auto_commit_interval not in [None, 0]:
            Timer(self.auto_commit_interval, self.run_auto_commit).start()

    def get_last_doc(self):
        """ Returns the last document stored in Algolia.
        """
        last_object_id = self.get_last_object_id()
        if last_object_id is None:
            return None
        try:
            return self.index.getObject(str(last_object_id))
        except algoliasearch.AlgoliaException as e:
            raise errors.ConnectionFailed(
                "Could not connect to Algolia Search: %s" % e)

    def get_last_object_id(self):
        try:
            return (self.index.getSettings().get('userData', {})).get('lastObjectID', None)
        except algoliasearch.AlgoliaException as e:
            raise errors.ConnectionFailed(
                "Could not connect to Algolia Search: %s" % e)
