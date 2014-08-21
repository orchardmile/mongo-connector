# Copyright 2013-2014 MongoDB, Inc.
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

"""Unit tests for the Algolia DocManager."""
import time
import sys
if sys.version_info[:2] == (2, 6):
    import unittest2 as unittest
else:
    import unittest
from tests import elastic_pair
from tests.test_algolia import AlgoliaTestCase

sys.path[0:0] = [""]

from mongo_connector.doc_managers.algolia_doc_manager import DocManager


class AlgoliaDocManagerTester(AlgoliaTestCase):
    """Unit tests for the Algolia DocManager."""

    def test_update(self):
        """Test the update method."""
        doc = {"_id": '1', "a": 1, "b": 2}
        self.algolia_doc.upsert(doc)
        self.algolia_doc.commit(True)
        # $set only
        update_spec = {"$set": {"a": 1, "b": 2}}
        self.algolia_doc.update(doc, update_spec)
        self.algolia_doc.commit(True)
        doc = self.algolia_index.getObject('1')
        self.assertEqual(doc, {"_id": '1', "objectID": '1', "a": 1, "b": 2})
        # $unset only
        update_spec = {"$unset": {"a": True}}
        self.algolia_doc.update(doc, update_spec)
        self.algolia_doc.commit(True)
        doc = self.algolia_index.getObject('1')
        self.assertEqual(doc, {"_id": '1', "objectID": '1', "b": 2, "a": None})
        # mixed $set/$unset
        update_spec = {"$unset": {"b": True}, "$set": {"c": 3}}
        self.algolia_doc.update(doc, update_spec)
        self.algolia_doc.commit(True)
        doc = self.algolia_index.getObject('1')
        self.assertEqual(doc, {"_id": '1', "objectID": '1', "c": 3, "a": None, "b": None})

    def test_upsert(self):
        """Test the upsert method."""
        docc = {'_id': '1', 'name': 'John'}
        self.algolia_doc.upsert(docc)
        self.algolia_doc.commit(True)
        res = self.algolia_index.search('')["hits"]
        for doc in res:
            self.assertEqual(doc['_id'], '1')
            self.assertEqual(doc['name'], 'John')

    def test_bulk_upsert(self):
        """Test the bulk_upsert method."""
        self.algolia_doc.bulk_upsert([])
        self.algolia_doc.commit(True)

        docs = ({"_id": i} for i in range(100))
        self.algolia_doc.bulk_upsert(docs)
        self.algolia_doc.commit(True)
        res = self.algolia_index.search('', { 'hitsPerPage': 101 })["hits"]
        returned_ids = sorted(int(doc["_id"]) for doc in res)
        self.assertEqual(len(returned_ids), 100)
        for i, r in enumerate(returned_ids):
            self.assertEqual(r, i)

        docs = ({"_id": i, "weight": 2*i} for i in range(100))
        self.algolia_doc.bulk_upsert(docs)
        self.algolia_doc.commit(True)

        res = self.algolia_index.search('', { 'hitsPerPage': 101 })["hits"]
        returned_ids = sorted(int(doc["weight"]) for doc in res)
        self.assertEqual(len(returned_ids), 100)
        for i, r in enumerate(returned_ids):
            self.assertEqual(r, 2*i)

    def test_remove(self):
        """Test the remove method."""
        docc = {'_id': '1', 'name': 'John'}
        self.algolia_doc.upsert(docc)
        self.algolia_doc.commit(True)
        res = self.algolia_index.search('')["hits"]
        self.assertEqual(len(res), 1)

        self.algolia_doc.remove(docc)
        self.algolia_doc.commit(True)
        res = self.algolia_index.search('')["hits"]
        self.assertEqual(len(res), 0)

    def test_get_last_doc(self):
        """Test the get_last_doc method.

        Make sure we can retrieve the document most recently modified from Algolia.
        """
        base = self.algolia_doc.get_last_doc()
        ts = base.get("_ts", 0) if base else 0
        docc = {'_id': '4', 'name': 'Hare'}
        self.algolia_doc.upsert(docc)
        docc = {'_id': '5', 'name': 'Tortoise'}
        self.algolia_doc.upsert(docc)
        docc = {'_id': '6', 'name': 'Mr T.'}
        self.algolia_doc.upsert(docc)
        self.algolia_doc.commit(True)

        self.assertEqual(self.algolia_index.search('')['nbHits'], 3)
        doc = self.algolia_doc.get_last_doc()
        self.assertEqual(doc['_id'], '6')

        docc = {'_id': '4', 'name': 'HareTwin'}
        self.algolia_doc.upsert(docc)
        self.algolia_doc.commit(True)

        doc = self.algolia_doc.get_last_doc()
        self.assertEqual(doc['_id'], '4')
        self.assertEqual(self.algolia_index.search('')['nbHits'], 3)

if __name__ == '__main__':
    unittest.main()
