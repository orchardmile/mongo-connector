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

"""Integration tests for mongo-connector + Algolia."""
"""Integration tests for mongo-connector + Elasticsearch."""
import base64
import os
import sys
import time

from algoliasearch import algoliasearch
from gridfs import GridFS

sys.path[0:0] = [""]

from tests import elastic_pair
from tests.setup_cluster import ReplicaSet
from mongo_connector.doc_managers.algolia_doc_manager import DocManager
from mongo_connector.connector import Connector
from mongo_connector.util import retry_until_ok
from tests.util import assert_soon
from tests import unittest

class AlgoliaTestCase(unittest.TestCase):
    """Base class for all Algolia TestCases."""

    @classmethod
    def setUpClass(cls):
        cls.algolia_client = algoliasearch.Client(os.environ['ALGOLIA_APPLICATION_ID'], os.environ['ALGOLIA_API_KEY'])
        cls.algolia_doc = DocManager('%s:%s:%s' % (os.environ['ALGOLIA_APPLICATION_ID'], os.environ['ALGOLIA_API_KEY'], 'test_mongo_connector'), auto_commit_interval=0)

    def setUp(self):
        self.algolia_index = self.algolia_client.initIndex('test_mongo_connector')
        self.algolia_index.clearIndex()
        res = self.algolia_index.setSettings({ 'hitsPerPage': 20 }) # work-around empty settings
        self.algolia_index.waitTask(res['taskID'])

    def tearDown(self):
        self.algolia_client.deleteIndex('test_mongo_connector')

if __name__ == '__main__':
    unittest.main()
