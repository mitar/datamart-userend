import requests
import pandas
import os
import json
import copy
import frozendict
import pandas as pd
import typing
import io
import logging
from multiprocessing import Pool

from d3m.container import Dataset as d3m_Dataset
from d3m.container import DataFrame as d3m_DataFrame
from d3m.base import utils as d3m_utils
from datamart_isi.cache.general_search_cache import GeneralSearchCache
from datamart_isi.cache.metadata_cache import MetadataCache
from datamart_isi.cache.wikidata_cache import QueryCache
from datamart_isi.cache.materializer_cache import MaterializerCache
from datamart_isi import config
from datamart_isi.utilities import connection
from SPARQLWrapper import SPARQLWrapper, JSON, POST, URLENCODED
from d3m.metadata.base import ALL_ELEMENTS

WIKIDATA_URI_TEMPLATE = config.wikidata_uri_template
EM_ES_URL = connection.get_es_fb_embedding_server_url()
Q_NODE_SEMANTIC_TYPE = config.q_node_semantic_type
logger = logging.getLogger(__name__)


# EM_ES_URL = config.em_es_url
# EM_ES_INDEX = config.em_es_index
# EM_ES_TYPE = config.em_es_type

class DownloadManager:
    @staticmethod
    def fetch_blacklist_nodes():
        query = """
            SELECT ?item ?itemLabel 
            WHERE 
            {
              ?item wdt:P31/wdt:P279* wd:Q12132.
              SERVICE wikibase:label { bd:serviceParam wikibase:language "[AUTO_LANGUAGE],en". }
            }
        """
        blacklist_nodes_set = set()
        result = QueryCache().get_result(query)
        for each_Q_node in result:
            blacklist_nodes_set.add(each_Q_node['item']['value'].split("/")[-1])
        logger.info("Following Q nodes are added to blacklist which will not be considered for wikidata search")
        logger.info(str(blacklist_nodes_set))
        return blacklist_nodes_set

    @staticmethod
    def fetch_fb_embeddings(q_nodes_list, target_q_node_column_name):
        # add vectors columns in wikifier_res
        qnodes = list(filter(None, q_nodes_list))
        qnode_uris = [WIKIDATA_URI_TEMPLATE.format(qnode) for qnode in qnodes]
        # do elastic search
        num_of_try = int(len(qnode_uris) / 1024) + 1 if len(qnode_uris) % 1024 != 0 else int(len(qnode_uris) / 1024)
        res = dict()
        for i in range(num_of_try):
            query = {
                'query': {
                    'terms': {
                        'key.keyword': qnode_uris[1024 * i:1024 * i + 1024]
                    }
                },
                "size": len(qnode_uris[1024 * i:1024 * i + 1024])
            }
            url = '{}/_search'.format(EM_ES_URL)
            resp = requests.get(url, json=query)
            if resp.status_code == 200:
                result = resp.json()
                hits = result['hits']['hits']
                for hit in hits:
                    source = hit['_source']
                    _qnode = source['key'].split('/')[-1][:-1]
                    res[_qnode] = ",".join(source['value'])

        # change to dataframe
        return_df = pandas.DataFrame()
        for key, val in res.items():
            each_result = dict()
            each_result["q_node"] = key
            vectors = val.split(',')
            for i in range(len(vectors)):
                if i < 10:
                    s = '00' + str(i)
                elif i < 100:
                    s = '0' + str(i)
                else:
                    s = str(i)
                v_name = "vector_" + s + "_of_qnode_with_" + target_q_node_column_name
                each_result[v_name] = float(vectors[i])
            return_df = return_df.append(each_result, ignore_index=True)

        return return_df

    @staticmethod
    def parse_geospatial_query(geo_variable):
        """
        Finding closest q-node for a (latitude, longitude) point
        :param geo_variable: dict
        :return: a qnode: str
        """
        geo_gra_dict = {'country': 'Q6256', 'state': 'Q7275', 'city': 'Q515', 'county': 'Q28575',
                        'postal_code': 'Q37447'}

        wikidata_server = connection.get_wikidata_server_url()
        qm_wikidata = SPARQLWrapper(wikidata_server)
        qm_wikidata.setReturnFormat(JSON)
        qm_wikidata.setMethod(POST)
        qm_wikidata.setRequestMethod(URLENCODED)

        results = None
        if "latitude" in geo_variable.keys() and "longitude" in geo_variable.keys():
            granularity = geo_gra_dict[geo_variable["granularity"]]
            radius = geo_variable["radius"]
            x, y = geo_variable["longitude"], geo_variable["latitude"]

            if x and y:
                # find closest Q nodes around a geospatial point from wikidata query
                sparql_query = "select distinct ?place where \n{\n  ?place wdt:P31/wdt:P279* wd:" + granularity + " .\n" \
                               + "SERVICE wikibase:around {\n ?place wdt:P625 ?location .\n" \
                               + "bd:serviceParam wikibase:center " + "\"Point(" + str(x) + " " + str(
                    y) + ")\"^^geo:wktLiteral .\n" \
                               + "bd:serviceParam wikibase:radius " + "\"" + str(radius) + "\" .\n" \
                               + "bd:serviceParam wikibase:distance ?dist. \n}\n" \
                               + "SERVICE wikibase:label { bd:serviceParam wikibase:language \"en\" }\n}\n" \
                               + "ORDER BY ASC(?dist) \n Limit 1 \n"
                try:
                    qm_wikidata.setQuery(sparql_query)
                    results = qm_wikidata.query().convert()['results']['bindings']
                except Exception as e:
                    logger.error("Query for " + str(geo_variable) + " failed!")
                    logger.debug(e, exc_info=True)

        qnode = ''
        if results:
            value = results[0]["place"]["value"]
            qnode = value.split('/')[-1]
            # logger.info("Qnode:" + qnode)

        return qnode

    @staticmethod
    def query_geospatial_wikidata(supplied_dataset, search_result, endpoint) -> d3m_Dataset:
        """
        Finding augment_geospatial_result in cache, if not exists, do multiprocessing to find qnodes for all geospatial points.
        :param supplied_dataset: d3m dataset
        :param search_result: dict
        :param endpoint: connection url
        :return: d3m dataset after augmentation
        """
        general_search_cache_manager = GeneralSearchCache(connection_url=endpoint)
        res_id, supplied_dataframe = d3m_utils.get_tabular_resource(dataset=supplied_dataset,
                                                                    resource_id=None,
                                                                    has_hyperparameter=False)
        search_result_str = json.dumps(search_result)
        # try cache first
        try:
            cache_key = general_search_cache_manager.get_hash_key(supplied_dataframe=supplied_dataframe,
                                                                  search_result_serialized=search_result_str)
            cache_result = general_search_cache_manager.get_cache_results(cache_key)
            if cache_result is not None:
                logger.info("Get augment results from memcache success!")
                return cache_result
        except Exception as e:
            cache_key = None

        latitude_index = search_result['metadata']['search_result']['latitude_index']
        longitude_index = search_result['metadata']['search_result']['longitude_index']
        radius = search_result['metadata']['search_result']['radius']
        gran = search_result['metadata']['search_result']['granularity']

        # set query information
        geo_variables_list = []
        for latitude, longitude in zip(supplied_dataframe.iloc[:, latitude_index], supplied_dataframe.iloc[:, longitude_index]):
            geo_variable = {"latitude": latitude, "longitude": longitude, "radius": radius, "granularity": gran}
            geo_variables_list.append(geo_variable)

        # get qnodes from multiple processing
        logger.debug("Start to query geospatial data")
        with Pool(os.cpu_count()) as p:
            qnodes = p.map(DownloadManager.parse_geospatial_query, geo_variables_list)
        logger.debug("Finished querying geospatial data")

        # augment qnodes in dataframe
        output_df = copy.copy(supplied_dataframe)
        lat_name, long_name = supplied_dataframe.columns[latitude_index], supplied_dataframe.columns[longitude_index]
        if qnodes and set(qnodes) != set(''):
            output_df["Geo_" + lat_name + "_" + long_name + "_" + gran + "_wikidata"] = qnodes
        else:
            logger.debug("No geospatial Qnodes!")

        # generate dataset
        output_ds = copy.copy(supplied_dataset)
        output_ds[res_id] = d3m_DataFrame(output_df, generate_metadata=False)

        # update metadata on column length
        selector = (res_id, ALL_ELEMENTS)
        old_meta = dict(output_ds.metadata.query(selector))
        old_meta_dimension = dict(old_meta['dimension'])
        old_meta_dimension['length'] = output_df.shape[1]
        old_meta['dimension'] = frozendict.FrozenOrderedDict(old_meta_dimension)
        new_meta = frozendict.FrozenOrderedDict(old_meta)
        output_ds.metadata = output_ds.metadata.update(selector, new_meta)

        # update qnode column's metadata
        selector = (res_id, ALL_ELEMENTS, output_df.shape[1] - 1)
        metadata = {"name": output_df.columns[-1],
                    "structural_type": str,
                    'semantic_types': (
                        "http://schema.org/Text",
                        "https://metadata.datadrivendiscovery.org/types/Attribute",
                        Q_NODE_SEMANTIC_TYPE
                    )}
        output_ds.metadata = output_ds.metadata.update(selector, metadata)

        # save to cache
        if cache_key:
            response = general_search_cache_manager.add_to_memcache(supplied_dataframe=supplied_dataframe,
                                                                    search_result_serialized=search_result_str,
                                                                    augment_results=output_ds,
                                                                    hash_key=cache_key
                                                                    )
            # save the augmented result's metadata if second augment is conducted
            MetadataCache.save_metadata_from_dataset(output_ds)
            if not response:
                logger.warning("Push augment results to results failed!")
            else:
                logger.info("Push augment results to memcache success!")
        return output_ds

    @staticmethod
    def get_sample_dataset(search_result: "DatamartSearchResult", return_as_url: bool = False) -> typing.Union[str, bool]:
        """
        function used to get the sample dataset part from given search results
        :param return_as_url: to return the sample dataset directly or return a url point to the sample dataset
        :param search_result: a DatamartSearchResult class object
        :return: a string indicate the result or a bool value.
                If this is a bool value "False", it means this dataset should not be not displayed
        """
        file_type = search_result.search_result['file_type']['value']
        if file_type == "csv":
            result = DownloadManager.get_sample_dataset_table_format(search_result)
        else:
            result = DownloadManager.get_sample_dataset_other_format(search_result)
        return result

    @staticmethod
    def get_sample_dataset_table_format(search_result: "DatamartSearchResult") -> typing.Union[str, bool]:
        """
        function used to get the sample dataset for table format data
        :param search_result: a DatamartSearchResult class object
        :return:
        """
        # for vector search results and wikidata search results, we need to fetch the results
        materialize_info = search_result.serialize()
        materialize_info_decoded = json.loads(materialize_info)
        search_type = materialize_info_decoded["metadata"]['search_type']
        sample_data = ""
        try:
            if search_type == "vector":
                q_nodes_list = materialize_info_decoded["metadata"]['search_result']['q_nodes_list'][:10]
                target_column_name = materialize_info_decoded["metadata"]['search_result']['target_q_node_column_name']
                first_10_rows_df = DownloadManager.fetch_fb_embeddings(q_nodes_list=q_nodes_list,
                                                                       target_q_node_column_name=target_column_name)
                # updated v2020.1.6: do not return this results if get nothing on first 10 rows
                if first_10_rows_df.shape[0] == 0:
                    return False
                sample_data = first_10_rows_df.to_csv(index=False)

            elif search_type == "wikidata":
                p_nodes = search_result.id().split("___")
                if p_nodes[-1].startswith("with_column"):
                    suffix_col_name = "_for_" + p_nodes[-1][12:]
                else:
                    suffix_col_name = ""
                p_nodes = p_nodes[1: -1]
                if len(p_nodes) == 0:
                    logger.error("No Q nodes given for this search result! Will skip")
                    return False
                materialize_info_wikidata = {
                    "p_nodes_needed": p_nodes,
                    "length": 10,
                    "suffix_col_name": suffix_col_name,
                    "show_item_label": True
                }
                temp_df = MaterializerCache.materialize(materialize_info_wikidata)
                # updated v2020.1.6: do not return this results if get nothing on first 10 rows
                if temp_df.shape[0] == 0 or temp_df.shape[1] == 0:
                    return False

                sample_data = temp_df.to_csv(index=False)

            elif search_type == "general":
                extra_info_json = json.loads(materialize_info_decoded["metadata"]["search_result"]["extra_information"]["value"])
                temp_df = pd.read_csv(io.StringIO(extra_info_json["first_10_rows"]))
                if 'Unnamed: 0' in temp_df.columns:
                    temp_df = temp_df.drop(columns=['Unnamed: 0'])
                sample_data = temp_df.to_csv(index=False)

        except Exception as e:
            logger.error("Fetching the first 10 rows failed!")
            logger.debug(e, exc_info=True)
            sample_data = ""
        return sample_data

    @staticmethod
    def get_sample_dataset_other_format(search_result: "DatamartSearchResult"):
        extra_information = json.loads(search_result.search_result['extra_information']['value'])
        sample_data = extra_information.get('first_10_rows')
        return sample_data

    @staticmethod
    def get_metadata(search_result) -> dict:
        """
            combine function to get metadata for 2 different method
        """
        file_type = search_result.search_result['file_type']['value']
        if file_type == "csv":
            metadata = search_result.get_metadata().to_json_structure()
        else:
            extra_information = json.loads(search_result.search_result['extra_information']['value'])
            metadata = {}
            for k, v in extra_information.items():
                if "meta" in k:
                    metadata[k] = v
        return metadata
