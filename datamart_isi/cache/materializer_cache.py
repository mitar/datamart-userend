from SPARQLWrapper import SPARQLWrapper, JSON, POST, URLENCODED
import memcache
import logging
import hashlib
import pickle
import datetime
import typing
import pandas as pd
import os
from datamart_isi import config
from datamart_isi.utilities import connection
from d3m.container import DataFrame as d3m_DataFrame
from pandas.util import hash_pandas_object

MEMCACHE_SERVER = config.memcache_server
WIKIDATA_QUERY_SERVER = config.wikidata_server
MEMCAHCE_MAX_VALUE_SIZE = config.memcache_max_value_size


class MaterializerCache(object):
    def __init__(self, connection_url: str, memcache_max_value_size=MEMCAHCE_MAX_VALUE_SIZE):
        self._logger = logging.getLogger(__name__)
        self.memcache_server = connection.get_memcache_server_url(connection_url)
        self.general_search_server = connection.get_genearl_search_server_url(connection_url)
        self._logger.debug("Current memcache server url is: " + self.memcache_server)
        self._logger.debug("Current general search server url is: " + self.general_search_server)
        try:
            self.mc = memcache.Client([self.memcache_server], debug=True, server_max_value_length=memcache_max_value_size)
            self._logger.info("Start memcache connection to " + self.memcache_server + " success!")
        except Exception as e:
            self.mc = None
            self._logger.error("Start memcache connection to " + self.memcache_server + " failed!")
            self._logger.debug(e, exc_info=True)

        self.qm = SPARQLWrapper(self.general_search_server)
        self.qm.setReturnFormat(JSON)
        self.qm.setMethod(POST)
        self.qm.setRequestMethod(URLENCODED)

    def get_cache_results(self, hash_key) -> typing.Optional[bytes]:
        """
        Function used to check whether this query hash tag exist in memcache system or not
        :param hash_key: the hash key of the query
        :return: the cached query in pickled format if the hash key hit, otherwise None
        """
        if self.mc is not None:
            # check whether we have cache or not
            results = self.mc.get("materializer_" + hash_key)
            if results:
                self._logger.info("Cache hit! will use this results.")
                try:
                    with open(results, "rb") as f:
                        results_loaded = pickle.load(f)
                    return results_loaded
                except Exception as e:
                    self._logger.warning("Hit results are broken! Need to rerun the query!")
                    self._logger.debug(e, exc_info=True)
            else:
                self._logger.info("Cache not hit.")
        else:
            self._logger.info("No memcache server connected, skip cache searching.")
        
        return None
            
    def add_to_memcache(self, materialized_dataframe, hash_key) -> bool:
        try:
            self._logger.debug("Start pushing general augment result to " + self.memcache_server)

            path_to_agument_results = os.path.join(config.cache_file_storage_base_loc, hash_key + ".pkl")
            with open(path_to_agument_results, "wb") as f:
                pickle.dump(materialized_dataframe, f)

            response_code1 = self.mc.set("materializer_" + hash_key, path_to_agument_results)
            if not response_code1:
                self._logger.warning("Pushing materializer result failed! Maybe the size too big?")

            # add timestamp to let the system know when to update
            response_code2 = self.mc.set("timestamp_" + hash_key, str(datetime.datetime.now().timestamp()))
            if not response_code2:
                self._logger.warning("Pushing timestamp failed! What happened???")

            # only return True if all success
            if response_code1 and response_code2:
                self._logger.info("Pushing search result success!")
                return True
            else:
                return False
        except Exception as e:
            self._logger.error("Pushing results of general search hash key " + hash_key + " failed!")
            self._logger.debug(e, exc_info=True)
            return False

    def get_hash_key(self, materialize_info: dict, run_wikifier: bool) -> str:
        """
        get the hash key for the this general search result
        :return: a str represent the hash key
        """
        materialize_info_fixed = {'url': materialize_info['url'],
                                  'file_type': materialize_info['file_type'],
                                  'extra_information': materialize_info['extra_information'],
                                  'run_wikifier': run_wikifier,
                                  }

        materialize_info_serialized = str(materialize_info_fixed)
        hash_generator = hashlib.md5()
        hash_generator.update(materialize_info_serialized.encode('utf-8'))
        hash_search_result = hash_generator.hexdigest()
        hash_key = str(hash_search_result)
        self._logger.debug("Current search's hash tag is " + hash_key)
        return hash_key
