import typing
import pandas as pd
import copy
import os
import random
import collections
import typing
import logging
import json
import re
import io
import string
import time
import cgitb
import sys
from ast import literal_eval
from itertools import combinations

from d3m import container
from d3m import utils
from d3m.container import DataFrame as d3m_DataFrame
from d3m.container import Dataset as d3m_Dataset
from d3m.base import utils as d3m_utils
from d3m.metadata.base import DataMetadata, ALL_ELEMENTS
from collections import defaultdict

from datamart import TabularVariable, ColumnRelationship, AugmentSpec
from datamart_isi import config
from datamart_isi.augment import Augment
from datamart_isi.joiners.rltk_joiner import RLTKJoinerGeneral
from datamart_isi.joiners.rltk_joiner import RLTKJoinerWikidata
from datamart_isi.utilities.utils import Utils
from datamart_isi.utilities.timeout import timeout_call
from datamart_isi.utilities.singleton import singleton
from datamart_isi.utilities import d3m_wikifier
from datamart_isi.utilities.d3m_metadata import MetadataGenerator
from datamart_isi.utilities.download_manager import DownloadManager
from datamart_isi.cache.wikidata_cache import QueryCache
from datamart_isi.cache.general_search_cache import GeneralSearchCache
from datamart_isi.cache.metadata_cache import MetadataCache
from datamart_isi.cache.materializer_cache import MaterializerCache

# from datamart_isi.joiners.join_result import JoinResult
# from datamart_isi.joiners.joiner_base import JoinerType


__all__ = ('DatamartQueryCursor', 'Datamart', 'DatasetColumn', 'DatamartSearchResult', 'AugmentSpec',
           'TabularJoinSpec', 'TemporalGranularity', 'ColumnRelationship', 'DatamartQuery',
           'VariableConstraint', 'TabularVariable', 'VariableConstraint')

Q_NODE_SEMANTIC_TYPE = config.q_node_semantic_type
TEXT_SEMANTIC_TYPE = config.text_semantic_type
ATTRIBUTE_SEMANTIC_TYPE = config.attribute_semantic_type
AUGMENTED_COLUMN_SEMANTIC_TYPE = config.augmented_column_semantic_type
TIME_SEMANTIC_TYPE = config.time_semantic_type

MAX_ENTITIES_LENGTH = config.max_entities_length
P_NODE_IGNORE_LIST = config.p_nodes_ignore_list
SPECIAL_REQUEST_FOR_P_NODE = config.special_request_for_p_nodes
AUGMENT_RESOURCE_ID = config.augmented_resource_id
DEFAULT_DATAMART_URL = config.default_datamart_url
TIME_COLUMN_MARK = config.time_column_mark
random.seed(42)


class DatamartQueryCursor(object):
    """
    Cursor to iterate through Datamarts search results.
    """

    def __init__(self, augmenter, search_query, supplied_data, need_run_wikifier=None, connection_url=None, **kwargs):
        """
        :param augmenter: The manager used to parse query and search on datamart general part(blaze graph),
                          because it search quick and need instance update, we should not cache this part
        :param search_query: query generated from Datamart class
        :param supplied_data: supplied data for search
        :param need_run_wikifier: an optional parameter, can help to control whether need to run wikifier to get
                                  wikidata-related parts, it can help to improve the speed when processing large data
        :param connection_url: control paramter for the connection url
        """
        self._logger = logging.getLogger(__name__)
        if connection_url:
            self._logger.info("Using user-defined connection url as " + connection_url)
            self.connection_url = connection_url
        else:
            connection_url = os.getenv('DATAMART_URL_ISI', DEFAULT_DATAMART_URL)
            self.connection_url = connection_url
        self.supplied_data = supplied_data
        if type(self.supplied_data) is d3m_Dataset:
            self.res_id, self.supplied_dataframe = d3m_utils.get_tabular_resource(dataset=self.supplied_data, resource_id=None)
        else:
            self.supplied_dataframe = self.supplied_data

        self._logger.debug("Current datamart connection url is: " + self.connection_url)
        self.augmenter = augmenter
        self.search_query = search_query
        self.current_searching_query_index = 0
        self.remained_part = None
        self.wikidata_cache_manager = QueryCache()
        self.q_nodes_columns = list()
        self.q_node_column_names = set()
        if need_run_wikifier is None:
            self.need_run_wikifier = self._check_need_wikifier_or_not()
        else:
            self.need_run_wikifier = need_run_wikifier
        self.consider_wikifier_columns_only = kwargs.get("consider_wikifier_columns_only", False)
        self.augment_with_time = kwargs.get("augment_with_time", False)
        self.consider_time = kwargs.get("consider_time", True)
        if self.consider_wikifier_columns_only:
            self._find_q_node_columns()
        self.search_limit_amount = 20

    def get_next_page(self, *, limit: typing.Optional[int] = 20, timeout: int = None) \
            -> typing.Optional[typing.Sequence['DatamartSearchResult']]:
        """
        Return the next page of results. The call will block until the results are ready.

        Note that the results are not ordered; the first page of results can be returned first simply because it was
        found faster, but the next page might contain better results. The caller should make sure to check
        `DatamartSearchResult.score()`.

        Parameters
        ----------
        limit : int or None
            Maximum number of search results to return. None means no limit.
        timeout : int
            Maximum number of seconds before returning results. An empty list might be returned if it is reached.

        Returns
        -------
        Sequence[DatamartSearchResult] or None
            A list of `DatamartSearchResult's, or None if there are no more results.
        """
        if timeout is None:
            timeout = 1800
        self._logger.info("Set time limit to be " + str(timeout) + " seconds.")

        if limit is not None:
            self.search_limit_amount = limit

        # if need to run wikifier, run it before any search
        if self.current_searching_query_index == 0 and self.need_run_wikifier:
            self.supplied_data = self.run_wikifier(self.supplied_data)

        # if already remained enough part
        current_result = self.remained_part or []
        if len(current_result) > limit:
            self.remained_part = current_result[limit:]
            current_result = current_result[:limit]
            return current_result

        # start searching
        while self.current_searching_query_index < len(self.search_query):
            time_start = time.time()
            self._logger.debug("Start searching on query No." + str(self.current_searching_query_index))

            if self.search_query[self.current_searching_query_index].search_type == "wikidata":
                # TODO: now wikifier can only automatically search for all possible columns and do exact match
                search_res = timeout_call(timeout, self._search_wikidata, [])
            elif self.search_query[self.current_searching_query_index].search_type == "general":
                search_res = timeout_call(timeout, self._search_datamart, [])
            elif self.search_query[self.current_searching_query_index].search_type == "vector":
                search_res = timeout_call(timeout, self._search_vector, [])
            elif self.search_query[self.current_searching_query_index].search_type == "geospatial":
                search_res = timeout_call(timeout, self._search_geospatial_data, [])
            else:
                raise ValueError("Unknown search query type for " +
                                 self.search_query[self.current_searching_query_index].search_type)

            time_used = (time.time() - time_start)
            timeout -= time_used
            if search_res is not None:
                self._logger.info("Running search on query No." + str(self.current_searching_query_index) + " used "
                                  + str(time_used) + " seconds and finished.")
                self._logger.info("Remained searching time: " + str(timeout) + " seconds.")
            elif timeout <= 0:
                self._logger.error(
                    "Running search on query No." + str(self.current_searching_query_index) + " timeout!")
                break
            else:
                self._logger.error("Running search on query No." + str(self.current_searching_query_index) + " failed!")

            self.current_searching_query_index += 1
            if search_res is not None:
                self._logger.info("Totally {} results found.".format(str(len(search_res))))
                current_result.extend(search_res)

        if len(current_result) == 0:
            self._logger.warning("No search results found!")
            return None
        else:
            current_result = sorted(current_result, key=lambda x: x.score(), reverse=True)
            if len(current_result) > limit:
                self.remained_part = current_result[limit:]
                current_result = current_result[:limit]
            return current_result

    def _check_need_wikifier_or_not(self) -> bool:
        """
        Check whether need to run wikifier or not, if wikidata type column detected, this column's semantic type will also be
        checked if no Q node semantic exist
        :return: a bool value
        True means Q nodes column already detected and skip running wikifier
        False means no Q nodes column detected, need to run wikifier
        """
        need_wikifier_or_not, self.supplied_data = d3m_wikifier.check_and_correct_q_nodes_semantic_type(self.supplied_data)
        if not need_wikifier_or_not:
            # if not need to run wikifier, we can find q node columns now
            self._find_q_node_columns()
        return need_wikifier_or_not

    def _find_q_node_columns(self) -> None:
        """
        Inner function used to find q node columns by semantic type
        :return: None
        """
        if len(self.q_nodes_columns) > 0 or len(self.q_node_column_names) > 0:
            self._logger.warning("Q node columns has already been found once! Should not run again")
            self.q_node_column_names = set()
            self.q_nodes_columns = list()

        if type(self.supplied_data) is d3m_Dataset:
            selector_base_type = "ds"
        else:
            selector_base_type = "df"

        # check whether Qnode is given in the inputs, if given, use this to search
        metadata_input = self.supplied_data.metadata
        for i in range(self.supplied_dataframe.shape[1]):
            if selector_base_type == "ds":
                metadata_selector = (self.res_id, ALL_ELEMENTS, i)
            else:
                metadata_selector = (ALL_ELEMENTS, i)
            if Q_NODE_SEMANTIC_TYPE in metadata_input.query(metadata_selector)["semantic_types"]:
                # if no required variables given, attach any Q nodes found
                self.q_nodes_columns.append(i)
                self.q_node_column_names.add(self.supplied_dataframe.columns[i])

    def _find_time_ranges(self) -> dict:
        """
        inner function that used to find the time information from search queries
        :return: a dict with start time, end time and time granularity
        """
        info = defaultdict(list)
        for i, each_search_query in enumerate(self.search_query):
            if each_search_query.search_type == "general":
                for each_variable in each_search_query.variables:
                    if each_variable.key.startswith(TIME_COLUMN_MARK):
                        start_time, end_time, granularity = each_variable.values.split("____")
                        info["start"].append(pd.to_datetime(start_time).isoformat())
                        info["end"].append(pd.to_datetime(end_time).isoformat())
                        info["granularity"].append(Utils.map_granularity_to_value(granularity))

        # if no time information found
        if len(info) == 0:
            return {}

        time_column_info = {
            "start": min(info["start"]),
            "end": max(info["end"]),
            "granularity": min(info["granularity"]),
        }
        return time_column_info

    def run_wikifier(self, input_data: d3m_Dataset) -> d3m_Dataset:
        """
        function used to run wikifier, and then return a d3m_dataset as the wikified results if success,
        otherwise return original input
        :return: None
        """
        self._logger.debug("Start running wikifier for supplied data in search...")
        results = d3m_wikifier.run_wikifier(supplied_data=input_data)
        self._logger.info("Wikifier running finished.")
        self.need_run_wikifier = False
        self._find_q_node_columns()
        return results

    def _search_wikidata(self, query=None, supplied_data: typing.Union[d3m_DataFrame, d3m_Dataset] = None,
                         search_threshold=0.5) -> typing.List["DatamartSearchResult"]:
        """
        The search function used for wikidata search
        :param query: JSON object describing the query.
        :param supplied_data: the data you are trying to augment.
        :param search_threshold: the minimum appeared times of the properties
        :return: list of search results of DatamartSearchResult
        """
        self._logger.debug("Start running search on wikidata...")
        if supplied_data is None:
            supplied_data = self.supplied_data

        wikidata_results = []
        try:
            if len(self.q_nodes_columns) == 0:
                self._logger.warning("No wikidata Q nodes detected on corresponding required_variables!")
                self._logger.warning("Will skip wikidata search part")
                return wikidata_results

            else:
                self._logger.info("Wikidata Q nodes inputs detected! Will search with it.")
                self._logger.info("Totally " + str(len(self.q_nodes_columns)) + " Q nodes columns detected!")

                # do a wikidata search for each Q nodes column
                for each_column in self.q_nodes_columns:
                    self._logger.debug("Start searching on column " + str(each_column))
                    q_nodes_list = self.supplied_dataframe.iloc[:, each_column].tolist()
                    p_count = collections.defaultdict(int)
                    p_nodes_needed = []
                    # old method, the generated results are not very good
                    """
                    http_address = 'http://minds03.isi.edu:4444/get_properties'
                    headers = {"Content-Type": "application/json"}
                    requests_data = str(q_nodes_list)
                    requests_data = requests_data.replace("'", '"')
                    r = requests.post(http_address, data=requests_data, headers=headers)
                    results = r.json()
                    for each_p_list in results.values():
                        for each_p in each_p_list:
                            p_count[each_p] += 1
                    """
                    # TODO: temporary change to call wikidata service, may change back in the future
                    # Q node format (wd:Q23)(wd: Q42)
                    q_node_query_part = ""
                    # ensure every time we get same order of q nodes so the hash tag will be same
                    unique_qnodes = set(q_nodes_list)
                    # updated v2020.1.7, use blacklist to filter q nodes
                    unique_qnodes = unique_qnodes - DownloadManager.fetch_blacklist_nodes()
                    unique_qnodes = list(unique_qnodes)
                    unique_qnodes.sort()
                    # updated v2020.1.6, not skip if unique Q nodes are too few
                    if len(unique_qnodes) == 0:
                        self._logger.warning("No Q nodes detected on column No.{} need to search, skip.".format(str(each_column)))
                        continue
                    if len(unique_qnodes) > config.max_q_node_query_size:
                        unique_qnodes = random.sample(unique_qnodes, config.max_q_node_query_size)

                    for each in unique_qnodes:
                        if len(each) > 0:
                            q_node_query_part += "(wd:" + each + ")"
                    sparql_query = "select distinct ?item ?property where \n{\n  VALUES (?item) {" + q_node_query_part \
                                   + "  }\n  ?item ?property ?value .\n  ?wd_property wikibase:directClaim ?property ." \
                                   + "  values ( ?type ) \n  {\n    ( wikibase:Quantity )\n" \
                                   + "    ( wikibase:Time )\n    ( wikibase:Monolingualtext )\n  }" \
                                   + "  ?wd_property wikibase:propertyType ?type .\n}\norder by ?item ?property "

                    results = self.wikidata_cache_manager.get_result(sparql_query)

                    if results is None:
                        # if response none, it means get wikidata query results failed
                        self._logger.error("Can't get wikidata search results for column No." + str(each_column) + "(" +
                                           self.supplied_dataframe.columns[each_column] + ")")
                        continue

                    self._logger.debug("Response from server for column No." + str(each_column) + "(" +
                                       self.supplied_dataframe.columns[each_column] + ")" +
                                       " received, start parsing the returned data from server.")
                    # count the appeared times and find the p nodes appeared  rate that higher than threshold
                    for each in results:
                        if "property" not in each:
                            self._logger.error("Wikidata query returned wrong results!!! Please check!!!")
                            raise ValueError("Wikidata query returned wrong results!!! Please check!!!")

                        p_count[each['property']['value'].split("/")[-1]] += 1

                    for key, val in p_count.items():
                        if float(val) / len(unique_qnodes) >= search_threshold:
                            p_nodes_needed.append(key)
                    wikidata_search_result = {"p_nodes_needed": p_nodes_needed,
                                              "target_q_node_column_name": self.supplied_dataframe.columns[each_column]}
                    wikidata_results.append(DatamartSearchResult(search_result=wikidata_search_result,
                                                                 supplied_data=supplied_data,
                                                                 query_json=query,
                                                                 search_type="wikidata")
                                            )

                self._logger.debug("Running search on wikidata finished.")
            return wikidata_results

        except Exception as e:
            self._logger.error("Searching with wikidata failed!")
            self._logger.debug(e, exc_info=True)

        finally:
            return wikidata_results

    def _search_datamart(self) -> typing.List["DatamartSearchResult"]:
        """
        function used for searching in datamart with blaze graph database
        :return: List[DatamartSearchResult]
        """
        self._logger.debug("Start searching on datamart...")
        search_result = []
        variables_search = self.search_query[self.current_searching_query_index].variables_search
        keywords_search = self.search_query[self.current_searching_query_index].keywords_search
        # COMMENT: title does not used, may delete later
        variables, title = dict(), dict()
        variables_temp = dict()  # this temp is specially used to store variable for time query
        if self.augment_with_time:
            time_information = self._find_time_ranges()
            if len(time_information) == 0:
                self._logger.warning("Required to search with time but no time column found from supplied data!")
                return []

        for each_variable in self.search_query[self.current_searching_query_index].variables:
            # updated v2019.12.11, now we only search "time column only" if augment_with_time is set to false
            if each_variable.key.startswith(TIME_COLUMN_MARK):
                if self.augment_with_time:
                    self._logger.warning("Not search with time only if augment_with_time is set to True")
                    return []
                elif self.consider_time is False:
                    self._logger.warning("Not search with time only if consider_time is set to False")
                    return []
                else:
                    variables_temp[each_variable.key.split("____")[1]] = each_variable.values
                    start_time, end_time, granularity = each_variable.values.split("____")
                    variables_search = {
                        "temporal_variable":
                            {
                                "start": start_time,
                                "end": end_time,
                                "granularity": granularity
                            }
                    }
            else:
                # updated v2019.12.18: if consider wikifier columns only, not search on other columns
                if self.consider_wikifier_columns_only and each_variable.key not in self.q_node_column_names:
                    self._logger.warning(
                        "Set to consider wikifier columns only, will not search for column {}".format(each_variable.key))
                    return []

                variables[each_variable.key] = each_variable.values

        query = {"keywords": self.search_query[self.current_searching_query_index].keywords,
                 "variables": variables,
                 "keywords_search": keywords_search,
                 "variables_search": variables_search,
                 }

        if self.augment_with_time:
            query["variables_time"] = time_information

        query_results = self.augmenter.query_by_sparql(query=query,
                                                       dataset=self.supplied_data,
                                                       consider_wikifier_columns_only=self.consider_wikifier_columns_only,
                                                       augment_with_time=self.augment_with_time,
                                                       limit_amount=self.search_limit_amount)

        if len(variables_temp) != 0:
            query["variables"] = variables_temp

        for i, each_result in enumerate(query_results):
            # self._logger.debug("Get returned No." + str(i) + " query result as ")
            # self._logger.debug(str(each_result))

            # the special way to calculate the score of temporal variable search
            if "start_time" in each_result.keys() and "end_time" in each_result.keys():
                if self.augment_with_time:
                    tv = time_information
                else:
                    tv = query["variables_search"]["temporal_variable"]
                start_date = pd.to_datetime(tv["start"]).timestamp()
                end_date = pd.to_datetime(tv["end"]).timestamp()  # query time
                start_time = pd.to_datetime(each_result['start_time']['value']).timestamp()
                end_time = pd.to_datetime(each_result['end_time']['value']).timestamp()  # dataset

                denominator = float(end_date - start_date)
                if end_date > end_time:
                    if start_date > end_time:
                        time_score = 0.0
                    elif start_date >= start_time and end_time >= start_date:
                        time_score = (end_time - start_date) / denominator
                    elif start_time > start_date:
                        time_score = (end_time - start_time) / denominator
                elif end_date >= start_time and end_time >= end_date:
                    if start_date >= start_time:
                        time_score = 1.0
                    elif start_time > start_date:
                        time_score = (end_date - start_time) / denominator
                elif start_time > end_date:
                    time_score = 0.0

                if time_score != 0.0 and 'score' in each_result.keys():
                    old_score = float(each_result['score']['value'])
                    each_result['score']['value'] = old_score + time_score
                else:
                    each_result['score'] = {"value": time_score}

            temp = DatamartSearchResult(search_result=each_result, supplied_data=self.supplied_data, query_json=query,
                                        search_type="general")
            search_result.append(temp)

        search_result.sort(key=lambda x: x.score(), reverse=True)
        self._logger.debug("Searching on datamart finished.")

        # need to add time on join pairs
        if self.augment_with_time:
            search_result = self._search_with_time_columns(search_result)

        return search_result

    def _search_vector(self) -> typing.List["DatamartSearchResult"]:
        """
        The search function used for vector search
        :return: List[DatamartSearchResult]
        """
        self._logger.debug("Start running search on Vectors...")
        vector_results = []
        try:
            if len(self.q_nodes_columns) == 0:
                self._logger.warning("No Wikidata Q nodes detected!")
                self._logger.warning("Will skip vector search part")
                return vector_results
            else:
                self._logger.info("Wikidata Q nodes inputs detected! Will search with it.")
                self._logger.info("Totally " + str(len(self.q_nodes_columns)) + " Q nodes columns detected!")

                # do a vector search for each Q nodes column
                for each_column in self.q_nodes_columns:
                    self._logger.debug("Start searching on column " + str(each_column))
                    q_nodes_list = list(filter(None, self.supplied_dataframe.iloc[:, each_column].dropna().tolist()))
                    unique_qnodes = list(set(q_nodes_list))
                    unique_qnodes.sort()
                    # updated v2020.1.6, not skip if unique Q nodes are too few
                    if len(unique_qnodes) < config.min_q_node_query_size_percent * len(q_nodes_list):
                        self._logger.warning("Too few Q nodes (rate = {}/{}) found on column {}, will skip this column.".
                                             format(str(len(unique_qnodes)),
                                                    str(config.min_q_node_query_size_percent * len(q_nodes_list)),
                                                    str(each_column)))
                        continue
                    vector_search_result = {"number_of_vectors": str(len(unique_qnodes)),
                                            "target_q_node_column_name": self.supplied_dataframe.columns[each_column],
                                            "q_nodes_list": unique_qnodes}
                    vector_results.append(DatamartSearchResult(search_result=vector_search_result,
                                                               supplied_data=self.supplied_data,
                                                               query_json=None,
                                                               search_type="vector")
                                          )

                self._logger.debug("Running search on vector finished.")
            return vector_results
        except Exception as e:
            self._logger.error("Searching with wikidata vector failed!")
            self._logger.debug(e, exc_info=True)

        finally:
            return vector_results

    def _search_geospatial_data(self) -> typing.List["DatamartSearchResult"]:
        """
        function used for searching geospatial data
        :return: List[DatamartSearchResult]
        """
        self._logger.debug("Start searching geospatial data on wikidata and datamart...")
        search_results = []

        # try to find possible columns of latitude and longitude
        possible_longitude_or_latitude = []
        for each in range(len(self.supplied_dataframe.columns)):
            if type(self.supplied_data) is d3m_Dataset:
                selector = (self.res_id, ALL_ELEMENTS, each)
            else:
                selector = (ALL_ELEMENTS, each)
            each_column_meta = self.supplied_data.metadata.query(selector)

            if "https://metadata.datadrivendiscovery.org/types/Location" in each_column_meta["semantic_types"]:
                try:
                    column_data = self.supplied_dataframe.iloc[:, each].astype(float).dropna()
                    if max(column_data) <= config.max_longitude_val and min(column_data) >= config.min_longitude_val:
                        possible_longitude_or_latitude.append(each)
                    elif max(column_data) <= config.max_latitude_val and min(column_data) >= config.min_latitude_val:
                        possible_longitude_or_latitude.append(each)
                except:
                    pass

        if len(possible_longitude_or_latitude) < 2:
            self._logger.debug("Supplied dataset does not have geospatial data!")
            return search_results
        else:
            self._logger.debug(
                "Finding columns:" + str(possible_longitude_or_latitude) + " which might be geospatial data columns...")

        possible_la_or_long_comb = list(combinations(possible_longitude_or_latitude, 2))
        for column_index_comb in possible_la_or_long_comb:
            latitude_index, longitude_index = -1, -1
            # try to get the correct latitude and longitude pairs
            for each_column_index in column_index_comb:
                try:
                    column_data = self.supplied_dataframe.iloc[:, each_column_index].astype(float).dropna()
                    column_name = self.supplied_dataframe.columns[each_column_index]

                    # must be longitude when its min is in [-180, -90), or max is in (90, 180]
                    if config.max_latitude_val < max(column_data) <= config.max_longitude_val \
                            or (config.min_latitude_val > min(column_data) >= config.min_longitude_val):
                        longitude_index = each_column_index
                    else:
                        # determine the type by header [latitude, longitude]
                        if any([True for i in column_name if i in ['a', 'A']]):
                            latitude_index = each_column_index
                        elif any([True for i in column_name if i in ['o', 'O', 'g', 'G']]):
                            longitude_index = each_column_index

                except Exception as e:
                    self._logger.debug(e, exc_info=True)
                    self._logger.error("Can't parse location information for column No." + str(each_column_index)
                                       + " with column name " + column_name)

            # search on datamart and wikidata by city qnodes
            if latitude_index != -1 and longitude_index != -1:
                self._logger.info(
                    "Latitude column is: " + str(latitude_index) + " and longitude is: " + str(longitude_index) + "...")
                granularity = {'city'}
                radius = 100

                for gran in granularity:
                    search_variables = {'metadata': {
                        'search_result': {
                            'latitude_index': latitude_index,
                            'longitude_index': longitude_index,
                            'radius': radius,
                            'granularity': gran
                        },
                        'search_type': 'geospatial'
                    }}
                    # do wikidata query service to find city q-node columns
                    return_ds = DownloadManager.query_geospatial_wikidata(self.supplied_data, search_variables,
                                                                          self.connection_url)
                    _, return_df = d3m_utils.get_tabular_resource(dataset=return_ds, resource_id=None)

                    if return_df.columns[-1].startswith('Geo_') and return_df.columns[-1].endswith('_wikidata'):
                        qnodes = return_df.iloc[:, -1]
                        qnodes_set = list(set(qnodes))
                        coverage_score = len(qnodes_set) / len(qnodes)

                        # search on datamart
                        qnodes_str = " ".join(qnodes_set)
                        variables = [VariableConstraint(key=return_df.columns[-1], values=qnodes_str)]
                        self.search_query[self.current_searching_query_index].variables = variables
                        search_res = timeout_call(1800, self._search_datamart, [])
                        search_results.extend(search_res)

                        # search on wikidata
                        temp_q_nodes_columns = self.q_nodes_columns
                        self.q_nodes_columns = [-1]
                        search_res = timeout_call(1800, self._search_wikidata, [None, return_df])
                        search_results.extend(search_res)
                        self.q_nodes_columns = temp_q_nodes_columns

        if search_results:
            for each_result in search_results:
                # change metadata's score
                old_score = each_result.score()
                new_score = old_score * coverage_score
                each_result.metadata_manager.score = new_score
                # change score in datamart_search_result
                if "score" in each_result.search_result.keys():
                    each_result.search_result["score"]["value"] = new_score

            search_results.sort(key=lambda x: x.score(), reverse=True)

        self._logger.debug("Running search on geospatial data finished.")
        return search_results

    def _search_with_time_columns(self, search_results: typing.List["DatamartSearchResult"]) \
            -> typing.List["DatamartSearchResult"]:
        """
        function used to update the search results from join with one column to join with both this column and time column
        :param search_results: list of "DatamartSearchResult"
        :return: list of "DatamartSearchResult"
        :return:
        """
        # find time columns first
        # get time ranges on supplied data
        time_columns_left = list()
        for i in range(self.supplied_dataframe.shape[1]):
            if type(self.supplied_data) is d3m_Dataset:
                each_selector = (self.res_id, ALL_ELEMENTS, i)
            else:
                each_selector = (ALL_ELEMENTS, i)
            each_column_metadata = self.supplied_data.metadata.query(each_selector)
            if "semantic_types" not in each_column_metadata:
                self._logger.warning("column No.{} {} do not have semantic type on metadata!".
                                     format(str(i), str(self.supplied_dataframe.columns[i])))
                continue

            if TIME_SEMANTIC_TYPE in each_column_metadata['semantic_types']:
                # if we got original time granularity from metadata, use it directly
                time_column = self.supplied_dataframe.iloc[:, i]
                if 'time_granularity' in each_column_metadata.keys():
                    granularity_d3m_format = each_column_metadata['time_granularity']
                    granularity = Utils.map_d3m_granularity_to_value(granularity_d3m_format['unit'])
                else:
                    try:
                        granularity_datamart_format = Utils.get_time_granularity(time_column)
                        granularity = Utils.map_granularity_to_value(granularity_datamart_format)
                    except ValueError:
                        self._logger.error("Can't continue because unable to get the time granularity on column No.{} {}".
                                           format(str(i), str(self.supplied_dataframe.columns[i])))
                        continue
                    self._logger.info("Get the time granularity of column No.{} {} as {}".
                                      format(str(i), str(self.supplied_dataframe.columns[i]), str(granularity)))
                if "datetime" not in time_column.dtype.name:
                    time_column = pd.to_datetime(time_column)
                time_columns_left.append({
                    "granularity": granularity,
                    "start_time": min(time_column),
                    "end_time": max(time_column),
                    "column_number": i,
                })

        # get time ranges on search results
        time_columns_right = list()
        for each_search_result in search_results:
            if each_search_result.search_type == "general":
                for i in range(each_search_result.d3m_metadata.query((ALL_ELEMENTS,))['dimension']['length']):
                    each_column_metadata = each_search_result.d3m_metadata.query((ALL_ELEMENTS, i))
                    # TODO: it seems our current system can't handle multiple time data's condition
                    if TIME_SEMANTIC_TYPE in each_column_metadata['semantic_types']:
                        time_information_query = self.augmenter.get_dataset_time_information(each_search_result.id())
                        if len(time_information_query) == 0:
                            self._logger.warning("Detect timestamp on dataset {} {} but no time information was found!"
                                                 .format(each_search_result.id(),
                                                         each_search_result.search_result['title']['value']))
                            continue

                        time_columns_right.append({
                            "granularity": int(time_information_query[0]['time_granularity']['value']),
                            "start_time": pd.Timestamp(time_information_query[0]['start_time']['value']),
                            "end_time": pd.Timestamp(time_information_query[0]['end_time']['value']),
                            "column_number": i,
                            "dataset_id": each_search_result.id()
                        })

        # only keep the datasets that has overlaped time range and same time granularity
        can_consider_datasets = defaultdict(list)
        for left_time_info in time_columns_left:
            for right_time_info in time_columns_right:
                left_range = [left_time_info['start_time'], left_time_info['end_time']]
                right_range = [right_time_info['start_time'], right_time_info['end_time']]
                # ensure the format are correct
                for i in range(len(left_range)):
                    if isinstance(left_range[i], pd.Timestamp):
                        left_range[i] = left_range[i].tz_localize('UTC')
                    elif isinstance(left_range[i], str):
                        left_range[i] = pd.Timestamp(left_range[i])

                # TODO: if time granularity different but time range overlap? should we consider it or not
                if left_time_info['granularity'] >= right_time_info['granularity'] and Utils.overlap(left_range, right_range):
                    can_consider_datasets[right_time_info['dataset_id']].append(
                        {
                            "left_column_number": left_time_info["column_number"],
                            "right_dataset_id": right_time_info['dataset_id'],
                            "right_join_column_number": right_time_info['column_number'],
                            "right_join_start_time": right_time_info['start_time'],
                            "right_join_end_time": right_time_info['end_time'],
                            "right_join_time_granularity": right_time_info['granularity']
                        })

        filtered_search_result = []
        for each_search_result in search_results:
            if each_search_result.search_type == "general":
                if each_search_result.id() in can_consider_datasets:
                    for each_combine in can_consider_datasets[each_search_result.id()]:
                        each_search_result_copied = copy.copy(each_search_result)
                        # update join pairs information
                        right_index = None
                        right_join_column_name = each_search_result.search_result['variableName']['value']
                        for i in range(each_search_result.d3m_metadata.query((ALL_ELEMENTS,))['dimension']['length']):
                            each_column_metadata = each_search_result.d3m_metadata.query((ALL_ELEMENTS, i))
                            if each_column_metadata['name'] == right_join_column_name:
                                right_index = i
                                break

                        if len(each_search_result.query_json['variables'].keys()) > 1:
                            self._logger.warning("Mutiple variables join results update for time related not supported yet!")
                        left_join_column_name = list(each_search_result.query_json['variables'].keys())[0]
                        left_index = self.supplied_dataframe.columns.tolist().index(left_join_column_name)
                        # right_index = right_df.columns.tolist().index(right_join_column_name)
                        original_left_index_column = DatasetColumn(resource_id=self.res_id, column_index=left_index)
                        original_right_index_column = DatasetColumn(resource_id=None, column_index=right_index)
                        left_columns = [
                            DatasetColumn(resource_id=self.res_id, column_index=each_combine["left_column_number"]),
                            original_left_index_column
                        ]
                        right_columns = [
                            DatasetColumn(resource_id=None, column_index=each_combine["right_join_column_number"]),
                            original_right_index_column
                        ]
                        updated_join_pairs = [TabularJoinSpec(left_columns=[left_columns], right_columns=[right_columns])]
                        each_search_result_copied.set_join_pairs(updated_join_pairs)
                        # update the search result with time information
                        time_search_keyword = TIME_COLUMN_MARK + "____" + right_join_column_name
                        each_search_result_copied.query_json['keywords'].append(time_search_keyword)
                        each_search_result_copied.search_result['start_time'] = str(each_combine["right_join_start_time"])
                        each_search_result_copied.search_result['end_time'] = str(each_combine["right_join_end_time"])
                        each_search_result_copied.search_result['time_granularity'] = str(
                            each_combine["right_join_time_granularity"])
                        filtered_search_result.append(each_search_result_copied)

        return filtered_search_result


@singleton
class Datamart(object):
    """
    ISI implement of datamart
    """

    def __init__(self, connection_url: str = None) -> None:
        self._logger = logging.getLogger(__name__)
        if connection_url:
            self._logger.info("Using user-defined connection url as " + connection_url)
            self.connection_url = connection_url
        else:
            connection_url = os.getenv('DATAMART_URL_ISI', DEFAULT_DATAMART_URL)
            self.connection_url = connection_url

        self._logger.debug("Current datamart connection url is: " + self.connection_url)
        self.augmenter = Augment()
        self.supplied_dataframe = None

    def search(self, query: 'DatamartQuery') -> DatamartQueryCursor:
        """This entry point supports search using a query specification.

        The query specification supports querying datasets by keywords, named entities, temporal ranges, and geospatial ranges.

        Datamart implementations should return a DatamartQueryCursor immediately.

        Parameters
        ----------
        query : DatamartQuery
            Query specification.

        Returns
        -------
        DatamartQueryCursor
            A cursor pointing to search results.
        """

        return DatamartQueryCursor(augmenter=self.augmenter, search_query=[query], supplied_data=None,
                                   connection_url=self.connection_url, need_run_wikifier=False)

    def search_with_data(self, query: 'DatamartQuery', supplied_data: container.Dataset, **kwargs) \
            -> DatamartQueryCursor:
        """
        Search using on a query and a supplied dataset.

        This method is a "smart" search, which leaves the Datamart to determine how to evaluate the relevance of search
        result with regard to the supplied data. For example, a Datamart may try to identify named entities and date
        ranges in the supplied data and search for companion datasets which overlap.

        To manually specify query constraints using columns of the supplied data, use the `search_with_data_columns()`
        method and `TabularVariable` constraints.

        Datamart implementations should return a DatamartQueryCursor immediately.

        Parameters
        ----------
        query : DatamartQuery
            Query specification
        supplied_data : container.Dataset
            The data you are trying to augment.
        kwargs : dict
            Some extra control parameters. For example:
            need_wikidata: (Default is True) If set to Ture, the program will run wikifier on supplied data and find possible
                Q nodes, then search for possible attributes with those Q nodes and search for vectors
            augment_with_time: (Default is False) If set to True, a pair with two columns will be searched, only data with
                both join columns like [time, key] will be considered
            consider_time: (Default is True) If set to True, no time columns on datamart will be considered as candidates.
                This control parameter will be useless if augment_with_time was True
            consider_wikifier_columns_only: (Default is False) If set to True, only columns with Q nodes will be considered
                as join candiadates

        Returns
        -------
        DatamartQueryCursor
            A cursor pointing to search results containing possible companion datasets for the supplied data.
        """
        # update v2019.10.24, add keywords search in search queries
        if query.keywords:
            query_keywords = []
            for each in query.keywords:
                translator = str.maketrans(string.punctuation, ' ' * len(string.punctuation))
                words_processed = str(each).lower().translate(translator).split()
                query_keywords.extend(words_processed)
        else:
            query_keywords = None

        need_wikidata = kwargs.get("need_wikidata", True)
        consider_wikifier_columns_only = kwargs.get("consider_wikifier_columns_only", False)
        augment_with_time = kwargs.get("augment_with_time", False)
        consider_time = kwargs.get("consider_time", True)
        if consider_time is False and augment_with_time is True:
            self._logger.warning("Augment with time is set to be true! consider_time parameter will be useless.")

        # add some special search query in the first search queries
        if not need_wikidata:
            search_queries = [DatamartQuery(search_type="geospatial")]
            need_run_wikifier = False
        else:
            need_run_wikifier = None
            search_queries = [DatamartQuery(search_type="wikidata"),
                              DatamartQuery(search_type="vector"),
                              DatamartQuery(search_type="geospatial")]

        # try to update with more correct metadata if possible
        updated_result = MetadataCache.check_and_get_dataset_real_metadata(supplied_data)
        if updated_result[0]:  # [0] store whether it success find the metadata
            supplied_data = updated_result[1]

        if type(supplied_data) is d3m_Dataset:
            res_id, self.supplied_dataframe = d3m_utils.get_tabular_resource(dataset=supplied_data, resource_id=None)
        else:
            raise ValueError("Incorrect supplied data type as " + str(type(supplied_data)))

        # if query is None:
        # if not query given, try to find the Text columns from given dataframe and use it to find some candidates
        can_query_columns = []
        for each in range(len(self.supplied_dataframe.columns)):
            if type(supplied_data) is d3m_Dataset:
                selector = (res_id, ALL_ELEMENTS, each)
            else:
                selector = (ALL_ELEMENTS, each)
            each_column_meta = supplied_data.metadata.query(selector)
            # try to parse each column to DateTime type. If success, add new semantic type, otherwise do nothing
            try:
                pd.to_datetime(self.supplied_dataframe.iloc[:, each])
                new_semantic_type = {"semantic_types": (TIME_SEMANTIC_TYPE, ATTRIBUTE_SEMANTIC_TYPE)}
                supplied_data.metadata = supplied_data.metadata.update(selector, new_semantic_type)
            except:
                pass

            if TEXT_SEMANTIC_TYPE in each_column_meta["semantic_types"] \
                    or TIME_SEMANTIC_TYPE in each_column_meta["semantic_types"]:
                can_query_columns.append(each)

        if len(can_query_columns) == 0:
            self._logger.warning("No column can be used for augment with datamart!")

        for each_column_index in can_query_columns:
            column_formated = DatasetColumn(res_id, each_column_index)
            tabular_variable = TabularVariable(columns=[column_formated], relationship=ColumnRelationship.CONTAINS)
            each_search_query = self.generate_datamart_query_from_data(supplied_data=supplied_data,
                                                                       data_constraints=[tabular_variable])
            # if we get keywords from input search query, add it
            if query_keywords:
                each_search_query.keywords_search = query_keywords
            search_queries.append(each_search_query)

        return DatamartQueryCursor(augmenter=self.augmenter, search_query=search_queries, supplied_data=supplied_data,
                                   need_run_wikifier=need_run_wikifier, connection_url=self.connection_url,
                                   consider_wikifier_columns_only=consider_wikifier_columns_only,
                                   augment_with_time=augment_with_time,
                                   consider_time=consider_time)

    def search_with_data_columns(self, query: 'DatamartQuery', supplied_data: container.Dataset,
                                 data_constraints: typing.List['TabularVariable']) -> DatamartQueryCursor:
        """
        Search using a query which can include constraints on supplied data columns (TabularVariable).

        This search is similar to the "smart" search provided by `search_with_data()`, but caller must manually specify
        constraints using columns from the supplied data; Datamart will not automatically analyze it to determine
        relevance or joinability.

        Use of the query spec enables callers to compose their own "smart search" implementations.

        Datamart implementations should return a DatamartQueryCursor immediately.

        Parameters
        ------_---
        query : DatamartQuery
            Query specification
        supplied_data : container.Dataset
            The data you are trying to augment.
        data_constraints : list
            List of `TabularVariable` constraints referencing the supplied data.

        Returns
        -------
        DatamartQueryCursor
            A cursor pointing to search results containing possible companion datasets for the supplied data.
        """

        # put entities of all given columns from "data_constraints" into the query's variable part and run the query

        # try to update with more correct metadata if possible
        updated_result = MetadataCache.check_and_get_dataset_real_metadata(supplied_data)
        if updated_result[0]:  # [0] store whether it success find the metadata
            supplied_data = updated_result[1]

        search_query = self.generate_datamart_query_from_data(supplied_data=supplied_data,
                                                              data_constraints=data_constraints)
        return DatamartQueryCursor(augmenter=self.augmenter, search_query=[search_query], supplied_data=supplied_data,
                                   connection_url=self.connection_url)

    def generate_datamart_query_from_data(self, supplied_data: container.Dataset,
                                          data_constraints: typing.List['TabularVariable']) -> "DatamartQuery":
        """
        Inner function used to generate the isi implemented datamart query from given dataset
        :param supplied_data: a Dataset format supplied data
        :param data_constraints:
        :return: a DatamartQuery can be used in isi datamart
        """
        all_query_variables = []
        keywords = []
        translator = str.maketrans(string.punctuation, ' ' * len(string.punctuation))

        for each_constraint in data_constraints:
            for each_column in each_constraint.columns:
                each_column_index = each_column.column_index
                each_column_res_id = each_column.resource_id
                all_value_str_set = set()
                each_column_meta = supplied_data.metadata.query((each_column_res_id, ALL_ELEMENTS, each_column_index))
                treat_as_a_text_column = False
                if TIME_SEMANTIC_TYPE in each_column_meta["semantic_types"]:
                    try:
                        column_data = supplied_data[each_column_res_id].iloc[:, each_column_index]
                        column_data_datetime_format = pd.to_datetime(column_data)
                        start_date = min(column_data_datetime_format)
                        end_date = max(column_data_datetime_format)
                        time_granularity = Utils.get_time_granularity(column_data_datetime_format)
                        # for time type, we create a special type of keyword and variables
                        # so that we can detect it later in general search part
                        each_keyword = TIME_COLUMN_MARK + "____" + supplied_data[each_column_res_id].columns[each_column_index]
                        keywords.append(each_keyword)
                        all_value_str = str(start_date) + "____" + str(end_date) + "____" + time_granularity
                        all_query_variables.append(VariableConstraint(key=each_keyword, values=all_value_str))

                    except Exception as e:
                        self._logger.debug(e, exc_info=True)
                        self._logger.error("Can't parse current datetime for column No." + str(each_column_index)
                                           + " with column name " + supplied_data[each_column_res_id].columns[each_column_index])
                        treat_as_a_text_column = True

                # for some special condition (DA_medical_malpractice), a column could have a DateTime tag but unable to be parsed
                # in such condition, we should search and treat it as a Text column then
                if 'http://schema.org/Text' in each_column_meta["semantic_types"] or treat_as_a_text_column:
                    column_values = supplied_data[each_column_res_id].iloc[:, each_column_index].astype(str)
                    query_column_entities = list(set(column_values.tolist()))
                    random.seed(42)  # ensure always get the same random number
                    if len(query_column_entities) > MAX_ENTITIES_LENGTH:
                        query_column_entities = random.sample(query_column_entities, MAX_ENTITIES_LENGTH)
                    for each in query_column_entities:
                        words_processed = str(each).lower().translate(translator).split()
                        for word in words_processed:
                            all_value_str_set.add(word)
                    all_value_str_list = list(all_value_str_set)
                    # ensure the order we get are always same
                    all_value_str_list.sort()
                    all_value_str = " ".join(all_value_str_list)
                    each_keyword = supplied_data[each_column_res_id].columns[each_column_index]
                    keywords.append(each_keyword)

                    all_query_variables.append(VariableConstraint(key=each_keyword, values=all_value_str))

        search_query = DatamartQuery(keywords=keywords, variables=all_query_variables)

        return search_query


class DatasetColumn:
    """
    Specify a column of a dataframe in a D3MDataset
    """

    def __init__(self, resource_id: typing.Optional[str], column_index: int) -> None:
        self.resource_id = resource_id
        self.column_index = column_index


class DatamartSearchResult:
    """
    This class represents the search results of a datamart search.
    Different datamarts will provide different implementations of this class.
    """

    def __init__(self, search_result: dict,
                 supplied_data: typing.Union[d3m_DataFrame, d3m_Dataset, None],
                 query_json: dict, search_type: str, connection_url: str = None):
        self._logger = logging.getLogger(__name__)
        self.search_result = search_result
        self.supplied_data = supplied_data
        if type(supplied_data) is d3m_Dataset:
            self.res_id, self.supplied_dataframe = d3m_utils.get_tabular_resource(dataset=supplied_data,
                                                                                  resource_id=None)
            self.selector_base_type = "ds"
        elif type(supplied_data) is d3m_DataFrame:
            self.res_id = None
            self.supplied_dataframe = supplied_data
            self.selector_base_type = "df"
        else:
            self.res_id = None
            self.supplied_dataframe = None

        if connection_url:
            self._logger.info("Using user-defined connection url as " + connection_url)
            self.connection_url = connection_url
        else:
            connection_url = os.getenv('DATAMART_URL_ISI', DEFAULT_DATAMART_URL)
            self.connection_url = connection_url

        self.wikidata_cache_manager = QueryCache()
        self.general_search_cache_manager = GeneralSearchCache()
        self.query_json = query_json
        self.search_type = search_type
        self.pairs = None
        self.join_pairs = None
        self.right_df = None
        extra_information = self.search_result.get('extra_information')
        if extra_information is not None:
            extra_information = json.loads(extra_information['value'])
            self.special_requirement = extra_information.get("special_requirement")
        else:
            self.special_requirement = None
        self.metadata_manager = MetadataGenerator(supplied_data=self.supplied_data, search_result=self.search_result,
                                                  search_type=self.search_type, connection_url=self.connection_url,
                                                  wikidata_cache_manager=self.wikidata_cache_manager)
        self.d3m_metadata = self.metadata_manager.generate_d3m_metadata_for_search_result()

    def _get_first_ten_rows(self) -> pd.DataFrame:
        """
        Inner function used to get first 10 rows of the search results
        :return:
        """
        return_res = ""
        try:
            if self.search_type == "general":
                return_res = json.loads(self.search_result['extra_information']['value'])['first_10_rows']

            elif self.search_type == "wikidata":
                materialize_info = self.search_result
                return_df = MaterializerCache.materialize(materialize_info, run_wikifier=False)
                return_df = return_df[:10]
                return_res = return_df.to_csv()

            elif self.search_type == "vector":
                sample_q_nodes = self.search_result["q_nodes_list"][:10]
                return_df = DownloadManager.fetch_fb_embeddings(sample_q_nodes, self.search_result["target_q_node_column_name"])
                return_res = return_df.to_csv(index=False)

            else:
                self._logger.error("unknown format of search result as {}!".format(str(self.search_type)))

        except Exception as e:
            self._logger.error("failed on getting first ten rows of search results")
            self._logger.debug(e, exc_info=True)
        finally:
            return return_res

    def display(self) -> pd.DataFrame:
        """
        function used to see what found inside this search result class in a human vision
        contains information for search result's title, columns and join hints
        :return: a pandas DataFrame
        """
        return self.metadata_manager.get_simple_view()

    def download(self, supplied_data: typing.Union[d3m_Dataset, d3m_DataFrame] = None,
                 connection_url: str = None, generate_metadata=True, return_format="ds", run_wikifier=True) \
            -> typing.Union[container.Dataset, container.DataFrame]:
        """
        Produces a D3M dataset (data plus metadata) corresponding to the search result.
        Every time the download method is called on a search result, it will produce the exact same columns
        (as specified in the metadata -- get_metadata), but the set of rows may depend on the supplied_data.
        Datamart is encouraged to return a dataset that joins well with the supplied data, e.g., has rows that match
        the entities in the supplied data. Datamarts may ignore the supplied_data and return the same data regardless.

        If the supplied_data is None, Datamarts may return None or a default dataset, based on the search query.

        Parameters
        ---------
        :param supplied_data : container.Dataset
            A D3M dataset containing the dataset that is the target for augmentation. Datamart will try to download data
            that augments the supplied data well.
        :param connection_url : str
            A connection string used to connect to a specific Datamart deployment. If not provided, the one provided to
            the `Datamart` constructor is used.
        :param generate_metadata: bool
            Whether need to get the auto-generated metadata or not, only valid in isi datamart
        :param return_format: str
            A control parameter to set which type of output should get, the default value is "ds" as dataset
            Optional choice is to get dataframe type output. Only valid in isi datamart
        :param run_wikifier： str
            A control parameter to set whether to run wikifier on this search result
        """
        if connection_url:
            # if a new connection url given
            if self.connection_url != connection_url:
                self.connection_url = connection_url
                self.wikidata_cache_manager = QueryCache()
                self.general_search_cache_manager = GeneralSearchCache()
                self.metadata_manager = MetadataGenerator(supplied_data=supplied_data, search_result=self.search_result,
                                                          search_type=self.search_type, connection_url=connection_url,
                                                          wikidata_cache_manager=self.wikidata_cache_manager)
                self._logger.info("New connection url given from download part as " + self.connection_url)

        if type(supplied_data) is d3m_Dataset:
            self.res_id, self.supplied_dataframe = d3m_utils.get_tabular_resource(dataset=supplied_data, resource_id=None)
        elif type(supplied_data) is d3m_DataFrame:
            self.supplied_dataframe = supplied_data
        else:
            self._logger.warning("No supplied data given, will try to use the exist one")
            if self.supplied_dataframe is None and self.supplied_data is None:
                raise ValueError("No supplied data found!")

        # get the results without metadata
        if self.search_type == "general":
            res = self._download_general(run_wikifier=run_wikifier)
        elif self.search_type == "wikidata":
            res = self._download_wikidata()
        elif self.search_type == "vector":
            res = self._download_vector()
        else:
            raise ValueError("Unknown search type with " + self.search_type)

        # sometime the index will be not continuous after augment, need to reset to ensure the index is continuous
        res.reset_index(drop=True)

        if return_format == "ds":
            return_df = d3m_DataFrame(res, generate_metadata=False)
            resources = {AUGMENT_RESOURCE_ID: return_df}
            return_result = d3m_Dataset(resources=resources, generate_metadata=False)
        elif return_format == "df":
            return_result = d3m_DataFrame(res, generate_metadata=False)
        else:
            raise ValueError("Invalid return format was given as " + str(return_format))

        if generate_metadata:
            return_result = self.metadata_manager.generate_metadata_for_download_result(return_result, supplied_data)

        return return_result

    def _download_general(self, run_wikifier) -> pd.DataFrame:
        """
        Specified download function for general datamart Datasets
        :return: a dataset or a dataframe depending on the input
        """
        self._logger.debug("Start downloading for datamart...")

        join_pairs_result = []
        candidate_join_column_scores = []

        # start finding pairs
        left_df = copy.deepcopy(self.supplied_dataframe)
        if self.right_df is None:
            self.right_df = MaterializerCache.materialize(metadata=self.search_result, run_wikifier=run_wikifier)
            right_df = self.right_df
        else:
            self._logger.info("Find downloaded data from previous time, will use that.")
            right_df = self.right_df
        self._logger.debug("Download finished, start finding pairs to join...")
        # left_metadata = Utils.generate_metadata_from_damax_entities_lengthtaframe(data=left_df, original_meta=None)
        # right_metadata = Utils.generate_metadata_from_dataframe(data=right_df, original_meta=None)

        if self.join_pairs is None:
            candidate_join_column_pairs = self.get_join_hints(left_df=left_df, right_df=right_df, left_df_src_id=self.res_id)
        else:
            candidate_join_column_pairs = self.join_pairs
        if len(candidate_join_column_pairs) > 1:
            logging.warning("multiple joining column pairs found! Will only check first one.")
        elif len(candidate_join_column_pairs) < 1:
            logging.error("Getting joining pairs failed")

        is_time_query = False
        if "start_time" in self.search_result and "end_time" in self.search_result:
            for each in self.query_json['keywords']:
                if TIME_COLUMN_MARK in each:
                    is_time_query = True
                    break

        if is_time_query:
            # if it is the dataset fond with time query, we should transform time column to same format and same granularity
            # then we can run RLTK with exact join same as str join
            time_granularity = self.search_result.get("time_granularity")
            if isinstance(time_granularity, str) or isinstance(time_granularity, int):
                time_granularity = int(time_granularity)
            elif isinstance(time_granularity, dict) and "value" in time_granularity:
                time_granularity = int(time_granularity["value"])
            elif time_granularity is None:
                # if not get time granularity, set as unknown, then try to get the real value
                self._logger.info("Unable to get time granularity! Will try to guess.")
                time_granularity = 8
            else:
                raise ValueError("Can't parse time granularity from {}".format(str(time_granularity)))

            if self.join_pairs is None:
                right_join_column_name = self.search_result['variableName']['value']
                right_df[right_join_column_name] = pd.to_datetime(right_df[right_join_column_name])
            else:
                join_pairs_numbers = self.join_pairs[0].get_column_number_pairs()
                time_stringfy_format = Utils.time_granularity_value_to_stringfy_time_format(time_granularity)
                if time_granularity == 8:
                    possible_granularity = {}
                for each_join_pair_left, each_join_pair_right in join_pairs_numbers:
                    for each_col in each_join_pair_left:
                        try:
                            left_df.iloc[:, each_col] = pd.to_datetime(left_df.iloc[:, each_col]) \
                                .dt.strftime(time_stringfy_format)
                            if time_granularity == 8:
                                possible_granularity[("left", each_col)] = \
                                    Utils.map_d3m_granularity_to_value(Utils.get_time_granularity(left_df.iloc[:, each_col]))
                        except:
                            self._logger.warning("Column No.{} {} on left_df(supplied_data) is not time column!".format(
                                str(each_col), left_df.columns[each_col]))

                    for each_col in each_join_pair_right:
                        try:
                            right_df.iloc[:, each_col] = pd.to_datetime(right_df.iloc[:, each_col]). \
                                dt.strftime(time_stringfy_format)
                            if time_granularity == 8:
                                possible_granularity[("right", each_col)] = \
                                    Utils.map_d3m_granularity_to_value(Utils.get_time_granularity(right_df.iloc[:, each_col]))
                        except:
                            self._logger.warning("Column No.{} {} on right_df is not time column!".format(
                                str(each_col), right_df.columns[each_col]))

                    # update with new time granularity
                    if time_granularity == "8":
                        time_granularity = min(possible_granularity.values())
                        self._logger.info("Get new granularity value as {}".format(str(time_granularity)))
                        time_stringfy_format = Utils.time_granularity_value_to_stringfy_time_format(time_granularity)
                        for k, v in possible_granularity.keys():
                            if k[0] == "left":
                                left_df.iloc[:, k[1]] = pd.to_datetime(left_df.iloc[:, k[1]]). \
                                    dt.strftime(time_stringfy_format)
                            elif k[0] == "right":
                                right_df.iloc[:, k[1]] = pd.to_datetime(right_df.iloc[:, k[1]]). \
                                    dt.strftime(time_stringfy_format)

        pairs = candidate_join_column_pairs[0].get_column_number_pairs()
        # generate the pairs for each join_column_pairs
        for each_pair in pairs:
            left_columns = each_pair[0]
            right_columns = each_pair[1]
            try:
                # Only profile the joining columns, otherwise it will be too slow:
                # left_metadata = Utils.calculate_dsbox_features(data=left_df, metadata=left_metadata,
                #                                                selected_columns=set(left_columns))
                #
                # right_metadata = Utils.calculate_dsbox_features(data=right_df, metadata=right_metadata,
                #                                                 selected_columns=set(right_columns))

                self._logger.info(" - start getting pairs for " + str(each_pair))
                right_df_copy = copy.deepcopy(right_df)

                result, self.pairs = RLTKJoinerGeneral.find_pair(left_df=left_df, right_df=right_df_copy,
                                                                 left_columns=[left_columns],
                                                                 right_columns=[right_columns],
                                                                 left_metadata=None,
                                                                 right_metadata=None)

                join_pairs_result.append(result)
                # TODO: figure out some way to compute the joining quality
                candidate_join_column_scores.append(1)

            except Exception as e:
                self._logger.error("failed when getting pairs for", each_pair)
                self._logger.debug(e, exc_info=True)

        # choose the best joining results
        all_results = []
        for i in range(len(join_pairs_result)):
            each_result = (pairs[i], candidate_join_column_scores[i], join_pairs_result[i])
            all_results.append(each_result)

        all_results.sort(key=lambda x: x[1], reverse=True)
        if len(all_results) == 0:
            raise ValueError("All join attempt failed!")

        return_result = all_results[0][2]
        self._logger.debug("download_general function finished.")
        return return_result

    def _dummy_download_wikidata(self) -> pd.DataFrame:
        """
        This function only should be used when the wikidata column on the search result is not found on supplied data
        This function will append same amount of blank columns to ensure the augmented data's column number and column names
        are same as normal condition
        :return: a DataFrame
        """
        # TODO: check if this can help to prevent fail on some corner case
        self._logger.warning("Adding empty wikidata columns!")
        p_nodes_needed = self.search_result["p_nodes_needed"]
        target_q_node_column_name = self.search_result["target_q_node_column_name"]
        specific_p_nodes_record = MetadataCache.get_specific_p_nodes(self.supplied_dataframe)
        columns_need_to_add = []
        # if specific_p_nodes_record is not None:
        #     for each_column in self.supplied_dataframe.columns:
        #         # if we find that this column should be wikified but not exist in supplied dataframe
        #         if each_column in specific_p_nodes_record and each_column + "_wikidata" not in self.supplied_dataframe.columns:
        #             columns_need_to_add.append(each_column + "_wikidata")
        for each_p_node in p_nodes_needed:
            each_p_node_name = Utils.get_node_name(each_p_node)
            columns_need_to_add.append(target_q_node_column_name + "_" + each_p_node_name)
        columns_need_to_add.append("joining_pairs")

        dummy_result = copy.copy(self.supplied_dataframe)
        for each_column in columns_need_to_add:
            dummy_result[each_column] = ""

        return dummy_result

    def _download_wikidata(self) -> pd.DataFrame:
        """
        :return: return_df: the materialized wikidata d3m_DataFrame,
                            with corresponding pairing information to original_data at last column
        """
        self._logger.debug("Start downloading for wikidata...")
        # prepare the query
        p_nodes_needed = self.search_result["p_nodes_needed"]
        target_q_node_column_name = self.search_result["target_q_node_column_name"]

        try:
            q_node_column_number = self.supplied_dataframe.columns.tolist().index(target_q_node_column_name)
        except ValueError:
            q_node_column_number = None
            self._logger.error("Could not find corresponding q node column for " + target_q_node_column_name +
                               ". It is possible that using wrong supplied data or wikified wrong columns before")

        if not q_node_column_number:
            return self._dummy_download_wikidata()

        q_nodes_list = set(self.supplied_dataframe.iloc[:, q_node_column_number].tolist())
        q_nodes_list = list(q_nodes_list)
        q_nodes_list.sort()
        p_nodes_needed.sort()
        q_nodes_query = ""
        p_nodes_query_part = ""
        p_nodes_optional_part = ""
        special_request_part = ""

        for each in q_nodes_list:
            if each != "N/A":
                q_nodes_query += "(wd:" + each + ") \n"
        for each in p_nodes_needed:
            if each not in P_NODE_IGNORE_LIST:
                p_nodes_query_part += " ?" + each
                p_nodes_optional_part += "  OPTIONAL { ?q wdt:" + each + " ?" + each + "}\n"
            if each in SPECIAL_REQUEST_FOR_P_NODE:
                special_request_part += SPECIAL_REQUEST_FOR_P_NODE[each] + "\n"

        sparql_query = "SELECT DISTINCT ?q " + p_nodes_query_part + \
                       " \nWHERE \n{\n  VALUES (?q) { \n " + q_nodes_query + "}\n" + \
                       p_nodes_optional_part + special_request_part + "}\n"

        results = self.wikidata_cache_manager.get_result(sparql_query)
        return_df = d3m_DataFrame()

        # if results is None, it means download failed, return blank dataFrame directly
        if results is None:
            # print 3 times to ensure easy to find
            self._logger.error("Download failed!!!")
            self._logger.error("Download failed!!!")
            self._logger.error("Download failed!!!")
            return return_df

        q_node_name_appeared = set()
        for result in results:
            each_result = {}
            q_node_name = result.pop("q")["value"].split("/")[-1]
            if q_node_name in q_node_name_appeared:
                continue
            q_node_name_appeared.add(q_node_name)
            each_result["q_node"] = q_node_name
            for p_name, p_val in result.items():
                each_result[p_name] = p_val["value"]
            return_df = return_df.append(each_result, ignore_index=True)

        column_name_update = dict()

        # for some special condition, we may meet this, and we need to ensure q_node column is the last column
        if return_df.columns[-1] != "q_node":
            cols = return_df.columns.tolist()
            cols.append(cols.pop(cols.index("q_node")))
            return_df = return_df[cols]

        # rename the columns from P node value to real name
        i = 0
        while len(self.d3m_metadata.query((ALL_ELEMENTS, i)).keys()) != 0:
            column_meta = self.d3m_metadata.query((ALL_ELEMENTS, i))
            if "P_node" in column_meta:
                column_name_update[column_meta['P_node']] = column_meta['name']
            i += 1

        return_df = return_df.rename(columns=column_name_update)

        # use rltk joiner to find the joining pairs
        joiner = RLTKJoinerWikidata()
        joiner.set_join_target_column_names((self.supplied_dataframe.columns[q_node_column_number], "q_node"))
        result, self.pairs = joiner.find_pair(left_df=self.supplied_dataframe, right_df=return_df)

        self._logger.debug("download_wikidata function finished.")
        return result

    def _download_vector(self) -> pd.DataFrame:
        """
        :return: return_df: the materialized vector d3m_DataFrame,
                            with corresponding pairing information to original_data at last column
        """
        self._logger.debug("Start downloading for vector...")
        target_q_node_column_name = self.search_result["target_q_node_column_name"]

        try:
            q_node_column_number = self.supplied_dataframe.columns.tolist().index(target_q_node_column_name)
        except ValueError:
            raise ValueError("Could not find corresponding q node column for " + target_q_node_column_name +
                             ". Maybe use the wrong search results?")
        q_nodes_list = set(self.supplied_dataframe.iloc[:, q_node_column_number].tolist())
        q_nodes_list = list(q_nodes_list)
        q_nodes_list.sort()

        return_df = DownloadManager.fetch_fb_embeddings(q_nodes_list, target_q_node_column_name)
        return_df = d3m_DataFrame(return_df)

        # use rltk joiner to find the joining pairs
        joiner = RLTKJoinerWikidata()
        joiner.set_join_target_column_names((self.supplied_dataframe.columns[q_node_column_number], "q_node"))
        result, self.pairs = joiner.find_pair(left_df=self.supplied_dataframe, right_df=return_df)

        self._logger.debug("download_vector function finished.")
        return result

    def _run_wikifier(self, supplied_data) -> d3m_Dataset:
        """
        Inner function to do wikifier type augment, this is purposed for doing augment with d3m primitive
        :param supplied_data:
        :return: a wikifiered d3m_Dataset if success
        """
        self._logger.debug("Start running wikifier.")
        # here because this part's code if for augment, we already have cache for that
        results = d3m_wikifier.run_wikifier(supplied_data=supplied_data, use_cache=True)
        self._logger.debug("Running wikifier finished.")
        return results

    def augment(self, supplied_data, augment_columns=None, connection_url=None, **kwargs):
        """
        Produces a D3M dataset that augments the supplied data with data that can be retrieved from this search result.
        The augment methods is a baseline implementation of download plus augment.

        Callers who want to control over the augmentation process should use the download method and use their own
        augmentation algorithm.

        This function actually do the concat steps that combine the joining pairs found from download and
        return a dataset with more columns. The detail pairs finding algorithm is located in download part
        Parameters
        ---------
        supplied_data : container.Dataset
            A D3M dataset containing the dataset that is the target for augmentation.
        augment_columns : typing.List[DatasetColumn]
            If provided, only the specified columns from the Datamart dataset that will be added to the supplied dataset.
        connection_url : str
            A connection string used to connect to a specific Datamart deployment. If not provided, a different
            deployment might be used.
        ---------
        Possible kwargs
        ---------
        augment_resource_id: str
            The augmented dataframe's resource id in return dataset
        use_cache: bool
        The augmented dataframe's resource id in return dataset
        """
        augment_resource_id = kwargs.get("augment_resource_id", AUGMENT_RESOURCE_ID)
        use_cache = kwargs.get("use_cache")
        if use_cache is None:
            use_cache = config.use_cache

        if type(supplied_data) is d3m_Dataset:
            # try to update with more correct metadata if possible
            updated_result = MetadataCache.check_and_get_dataset_real_metadata(supplied_data)
            if updated_result[0]:  # [0] store whether it success find the metadata
                supplied_data = updated_result[1]
            self.supplied_data = supplied_data
            self.res_id, self.supplied_dataframe = d3m_utils.get_tabular_resource(dataset=supplied_data,
                                                                                  resource_id=None,
                                                                                  has_hyperparameter=False)

        else:
            self.supplied_dataframe = supplied_data

        if connection_url:
            self._logger.info("Using user-defined connection url as " + connection_url)
            self.connection_url = connection_url
        else:
            connection_url = os.getenv('DATAMART_URL_ISI', DEFAULT_DATAMART_URL)
            self.connection_url = connection_url

        # ensure this supplied dataframe will not changed afterwards
        supplied_dataframe_original = copy.copy(self.supplied_dataframe)

        if use_cache:
            try:
                cache_key = self.general_search_cache_manager.get_hash_key(supplied_dataframe=supplied_dataframe_original,
                                                                           search_result_serialized=self.serialize())

                cache_result = self.general_search_cache_manager.get_cache_results(cache_key)
                if cache_result is not None:
                    if type(cache_result) is string:
                        self._logger.warning("This augment was failed last time!")
                        raise ValueError(
                            "Augment appeared to be failed during last execution with messsage \n" + str(cache_result))
                    else:
                        self._logger.info("Using caching results")
                        return cache_result

            except Exception as e:
                cache_key = None
                self._logger.error("Some error happened when getting results from cache! Will ignore the cache")
                self._logger.debug(e, exc_info=True)
        else:
            cache_key = None

        self._logger.info("Cache not hit or cache not used, start running augment.")

        try:
            if self.search_type == "wikifier":
                res = timeout_call(1800, self._run_wikifier, [supplied_data])
                # res = self._run_wikifier(supplied_data)

            else:
                if type(supplied_data) is d3m_DataFrame:
                    res = timeout_call(1800, self._augment, [supplied_data, augment_columns, True, "df", augment_resource_id])

                    # res = self._augment(supplied_data=supplied_data, augment_columns=augment_columns, generate_metadata=True,
                    #                     return_format="df", augment_resource_id=augment_resource_id)
                elif type(supplied_data) is d3m_Dataset:
                    res = timeout_call(1800, self._augment, [supplied_data, augment_columns, True, "ds", augment_resource_id])
                    # res = self._augment(supplied_data=supplied_data, augment_columns=augment_columns, generate_metadata=True,
                    #                     return_format="ds", augment_resource_id=augment_resource_id)
                else:
                    raise ValueError("Unknown input type for supplied data as: " + str(type(supplied_data)))

            if res is not None:
                # sometime the index will be not continuous after augment, need to reset to ensure the index is continuous
                res[augment_resource_id].reset_index(drop=True)
                res[augment_resource_id].fillna('', inplace=True)
                res[augment_resource_id] = res[augment_resource_id].astype(str)
            else:
                res = "failed because nothing returned, maybe because timeout?"

        except Exception as e:
            self._logger.error("Augment failed!")
            self._logger.debug(e, exc_info=True)
            info = sys.exc_info()
            res = str(cgitb.text(info))

        # should not cache wikifier results here, as we already cached it in wikifier part
        # and we don't know if the wikifier success or not here
        if use_cache and cache_key and self.search_type != "wikifier":
            # FIXME: should we cache failed results here?
            response = self.general_search_cache_manager.add_to_memcache(supplied_dataframe=supplied_dataframe_original,
                                                                         search_result_serialized=self.serialize(),
                                                                         augment_results=res,
                                                                         hash_key=cache_key
                                                                         )
            # save the augmented result's metadata if second augment is conducted
            if type(res) is not string:
                MetadataCache.save_metadata_from_dataset(res)
            if not response:
                self._logger.warning("Push augment results to results failed!")
            else:
                self._logger.info("Push augment results to memcache success!")

        # updated v2019.10.30, now raise the error instead of return the error
        if type(res) is string:
            raise ValueError(res)
        return res

    def _augment(self, supplied_data, augment_columns=None, generate_metadata=True, return_format="ds",
                 augment_resource_id=AUGMENT_RESOURCE_ID):
        """
        Inner detail function for augment part
        """
        self._logger.debug("Start running augment function.")
        if type(return_format) is not str or return_format != "ds" and return_format != "df":
            raise ValueError("Unknown return format as" + str(return_format))

        if type(supplied_data) is d3m_Dataset:
            supplied_data_df = copy.copy(supplied_data[self.res_id])
        elif type(supplied_data) is d3m_DataFrame:
            supplied_data_df = copy.copy(supplied_data)
        else:
            supplied_data_df = copy.copy(self.supplied_dataframe)

        if supplied_data_df is None:
            raise ValueError("Can't find supplied data!")

        download_result = self.download(supplied_data=supplied_data_df, generate_metadata=False, return_format="df")

        join_pair_column = download_result['joining_pairs']
        download_result = download_result.drop(columns=['joining_pairs'])
        left_pairs_oversize = False
        right_pairs_oversize = False
        start = time.time()

        # only need to check duplicate for general search's join
        if self.search_type == "general":
            maximum_accept_duplicate_amount = self.supplied_dataframe.shape[0] / 20
            self._logger.info("Maximum accept duplicate amount is: " + str(maximum_accept_duplicate_amount))
            for each_row in join_pair_column:
                if len(each_row) >= maximum_accept_duplicate_amount:
                    raise ValueError(
                        "Too much available join columns ({}) for pair {}".format(str(len(each_row)), self.join_pairs))

            left_pairs = defaultdict(list)
            right_pairs = defaultdict(list)

            for r1, r2 in self.pairs:
                if left_pairs_oversize and right_pairs_oversize:
                    raise ValueError("Should not augment for n-m relationship.")
                # otherwise continue counting
                if len(left_pairs[int(r1)]) >= maximum_accept_duplicate_amount:
                    left_pairs_oversize = True
                    left_pairs[int(r1)].append(int(r2))
                if len(right_pairs[int(r2)]) >= maximum_accept_duplicate_amount:
                    right_pairs_oversize = True
                elif not right_pairs_oversize:
                    right_pairs[int(r2)].append(int(r1))

        if left_pairs_oversize and right_pairs_oversize:
            # if n_to_m_condition
            raise ValueError("Should not augment for n-m relationship.")
            # df_joined = supplied_data_df

        else:
            # if we get special requirement from upload and not get from user side, use upload's
            if self.special_requirement and self.special_requirement.get("display_columns") and len(augment_columns) == 0:
                for each_col in self.special_requirement["display_columns"]:
                    augment_columns.append(DatasetColumn(resource_id=None, column_index=each_col))

            # add extra index column to ensure they follow the original order
            supplied_data_df['**original_index**'] = supplied_data_df.index
            # self.pairs = sorted(self.pairs, key=lambda x: int(x[0]))
            i = 0
            r1_paired = set()
            df_dict = dict()
            columns_new = None
            column_names_to_join = None
            for r1, r2 in self.pairs:
                i += 1
                r1_int = int(r1)
                if r1_int in r1_paired:
                    continue
                r1_paired.add(r1_int)
                left_res = supplied_data_df.loc[r1_int]
                right_res = download_result.loc[int(r2)]
                if column_names_to_join is None:
                    column_names_to_join = right_res.index.difference(left_res.index)
                    if self.search_type == "general":
                        # only for general search condition, we should remove the target join columns
                        right_join_column_name = self.search_result['variableName']['value']
                        if right_join_column_name in column_names_to_join:
                            column_names_to_join = column_names_to_join.drop(right_join_column_name)
                    # if specified augment columns given, only append these columns
                    if augment_columns:
                        augment_columns_with_column_names = []
                        max_length = self.d3m_metadata.query((ALL_ELEMENTS,))['dimension']['length']
                        for each in augment_columns:
                            if each.column_index < max_length:
                                each_column_meta = self.d3m_metadata.query((ALL_ELEMENTS, each.column_index))
                                augment_columns_with_column_names.append(each_column_meta["name"])
                            else:
                                self._logger.error("Index out of range, will ignore: " + str(each.column_index))
                        column_names_to_join = column_names_to_join.intersection(augment_columns_with_column_names)

                    columns_new = left_res.index.tolist()
                    columns_new.extend(column_names_to_join.tolist())
                dcit_right = right_res[column_names_to_join].to_dict()
                dict_left = left_res.to_dict()
                dcit_right.update(dict_left)
                df_dict[i] = dcit_right

            df_joined = pd.DataFrame.from_dict(df_dict, "index")
            # add up the rows don't have pairs
            unpaired_rows = set(range(supplied_data_df.shape[0])) - r1_paired
            if len(unpaired_rows) > 0:
                unpaired_rows_list = [i for i in unpaired_rows]
                df_joined = df_joined.append(supplied_data_df.iloc[unpaired_rows_list, :], ignore_index=True)

            # ensure that the original dataframe columns are at the first left part
            if columns_new is not None:
                df_joined = df_joined[columns_new]
            else:
                self._logger.error("Attention! It seems augment do not add any extra columns!")

            # if search with wikidata, we should remove duplicate Q node column
            self._logger.info("Join finished, totally take " + str(time.time() - start) + " seconds.")

            # sort the joined dataframe with original index
            df_joined = df_joined.sort_values(by='**original_index**')
            df_joined = df_joined.drop(columns=['**original_index**'])
        # END augment part

        if 'q_node' in df_joined.columns:
            df_joined = df_joined.drop(columns=['q_node'])

        if 'id' in df_joined.columns:
            df_joined = df_joined.drop(columns=['id'])

        # start adding column metadata for dataset
        if generate_metadata:
            return_result = self.metadata_manager.generate_metadata_for_augment_result(df_joined=df_joined,
                                                                                       return_format=return_format,
                                                                                       supplied_data=supplied_data,
                                                                                       augment_resource_id=augment_resource_id
                                                                                       )
        else:
            if return_format == "ds":
                self._logger.warning("It is useless to return a dataset without metadata!!!")
                return_df = d3m_DataFrame(df_joined, generate_metadata=False)
                resources = {augment_resource_id: return_df}
                return_result = d3m_Dataset(resources=resources, generate_metadata=False)
            else:
                return_result = d3m_DataFrame(df_joined)

        self._logger.debug("Augment finished")
        return return_result

    def score(self) -> float:
        return self.metadata_manager.score

    def id(self) -> str:
        return self.metadata_manager.id

    def get_metadata(self) -> DataMetadata:
        return self.d3m_metadata

    def set_join_pairs(self, join_pairs: typing.List["TabularJoinSpec"]) -> None:
        """
        manually set up the join pairs
        :param join_pairs: user specified TabularJoinSpec
        :return:
        """
        self.join_pairs = join_pairs

    def get_join_hints(self, left_df, right_df, left_df_src_id=None, right_src_id=None) -> typing.List["TabularJoinSpec"]:
        """
        Returns hints for joining supplied data with the data that can be downloaded using this search result.
        In the typical scenario, the hints are based on supplied data that was provided when search was called.

        The optional supplied_data argument enables the caller to request recomputation of join hints for specific data.

        :return: a list of join hints. Note that datamart is encouraged to return join hints but not required to do so.
        """
        self._logger.debug("Start getting join hints.")

        if self.search_type == "general":
            results = []
            left_col_number = None
            right_col_number = None
            if left_df is None or right_df is None:
                try:
                    join_left_cols = []
                    for each_key, each_value in literal_eval(self.search_result['extra_information']['value']).items():
                        if 'name' in each_value.keys() and each_value['name'] == self.search_result['variableName']['value']:
                            right_col_number = int(each_key.split("_")[-1])
                            break
                    join_right_cols = [DatasetColumn(resource_id=None, column_index=right_col_number)]
                    if self.supplied_dataframe is None:
                        self._logger.error(
                            "Can't get supplied dataframe information, failed to find the left join column number")
                    else:
                        for each in self.query_json['variables'].keys():
                            left_col_number = self.supplied_dataframe.columns.tolist().index(each)
                            join_left_cols.append(DatasetColumn(resource_id=self.res_id, column_index=left_col_number))
                    results.append(TabularJoinSpec(left_columns=[join_left_cols], right_columns=[join_right_cols]))
                except KeyError:
                    self._logger.warning("Can't find join columns! Maybe this search result is from search_without_data?")
                except Exception as e:
                    self._logger.error("Can't find join columns! Unknown error!")
                    self._logger.debug(e, exc_info=True)
                finally:
                    return results
            else:
                right_join_column_name = self.search_result['variableName']['value']
                left_columns = []
                right_columns = []
                for each in self.query_json['variables'].keys():
                    left_col_number = left_df.columns.tolist().index(each)
                    right_col_number = right_df.columns.tolist().index(right_join_column_name)
                    left_index_column = DatasetColumn(resource_id=left_df_src_id, column_index=left_col_number)
                    right_index_column = DatasetColumn(resource_id=right_src_id, column_index=right_col_number)
                    left_columns.append([left_index_column])
                    right_columns.append([right_index_column])

                results.append(TabularJoinSpec(left_columns=left_columns, right_columns=right_columns))
        else:
            raise ValueError("Type {} is not supported to get join hints.".format(self.search_type))

        self._logger.debug("Get join hints finished, the join hints are:")
        self._logger.debug(str(left_col_number) + ", " + str(right_col_number))
        return results

    def serialize(self) -> str:
        """
        Return a string format's json which contains all information needed for reproducing the augment
        :return:
        """
        result = dict()
        result['id'] = self.id()
        result['score'] = self.score()

        result['metadata'] = dict()
        result['metadata']['connection_url'] = self.connection_url
        result['metadata']['search_result'] = self.search_result
        result['metadata']['query_json'] = self.query_json
        result['metadata']['search_type'] = self.search_type
        augmentation = dict()
        augmentation['properties'] = "join"

        if self.search_type == "general":
            if not self.join_pairs:
                self.join_pairs = self.get_join_hints(left_df=self.supplied_dataframe, right_df=self.right_df)

            if len(self.join_pairs) == 0 or self.supplied_dataframe is None:
                self._logger.error("Fail to get the join pairs!")
                augmentation['left_columns'] = None
                augmentation['right_columns'] = None
            else:
                if len(self.join_pairs) > 1:
                    self._logger.warning("Multiple join pairs serialization not support yet!")
                join_pair = self.join_pairs[0]
                join_pair_numbers = join_pair.get_column_number_pairs()
                left_join_pair_numbers = []
                right_join_pair_numbers = []
                temp_df = pd.read_csv(io.StringIO(json.loads(self.search_result['extra_information']['value'])["first_10_rows"]))
                if 'Unnamed: 0' in temp_df.columns:
                    temp_df = temp_df.drop(columns=['Unnamed: 0'])
                for each_join_pair_numbers in join_pair_numbers:
                    left_join_pair_numbers.append(each_join_pair_numbers[0])
                    right_join_pair_numbers.append(each_join_pair_numbers[1])
                    col_names = []
                    for each_col in each_join_pair_numbers[1]:
                        # if this col number larger than first 10 rows results, it must be a wikifiered column
                        if each_col > temp_df.shape[1]:
                            col_names.append(self.search_result['variableName']['value'])
                        else:
                            col_names.append(temp_df.columns[each_col])
                    # right_join_pair_names.append(col_names)

                augmentation['left_columns'] = left_join_pair_numbers
                augmentation['right_columns'] = right_join_pair_numbers
                # augmentation['left_columns_names'] = left_join_pair_names
                # augmentation['right_columns_names'] = right_join_pair_names

        # otherwise try to guess from information
        elif self.search_type == "wikidata":
            left_col_number = self.supplied_dataframe.columns.tolist().index(self.search_result['target_q_node_column_name'])
            # left_col_name = self.search_result['target_q_node_column_name']
            augmentation['left_columns'] = [[left_col_number]]
            # augmentation['left_columns_names'] = [[left_col_name]]
            right_col_number = len(self.search_result['p_nodes_needed']) + 1
            augmentation['right_columns'] = [[right_col_number]]
            # augmentation['right_columns_names'] = [["q_node"]]

        elif self.search_type == "vector":
            left_col_number = self.supplied_dataframe.columns.tolist().index(self.search_result['target_q_node_column_name'])
            augmentation['left_columns'] = [[left_col_number]]
            # left_col_name = self.search_result['target_q_node_column_name']
            # augmentation['left_columns_names'] = [[left_col_name]]
            right_col_number = len(self.search_result['number_of_vectors'])  # num of rows, not columns
            augmentation['right_columns'] = [[right_col_number]]
            # augmentation['right_columns_names'] = [["q_node"]]

        if self.supplied_dataframe is not None:
            left_join_pair_names = self.supplied_dataframe.columns.tolist()
        else:
            left_join_pair_names = []

        right_join_pair_names = self.metadata_manager.get_column_names_from_metadata()
        result['dataframe_column_names'] = {
            "left_names": left_join_pair_names,
            "right_names": right_join_pair_names
        }
        result['augmentation'] = augmentation
        result['datamart_type'] = 'isi'
        result_str = json.dumps(result)

        return result_str

    @classmethod
    def deserialize(cls, serialize_result_str):
        serialize_result = json.loads(serialize_result_str)
        if "datamart_type" not in serialize_result or serialize_result["datamart_type"] != "isi":
            raise ValueError("False datamart type found")
        supplied_data = None  # serialize_result['metadata']['supplied_data']
        search_result = serialize_result['metadata']['search_result']
        query_json = serialize_result['metadata']['query_json']
        search_type = serialize_result['metadata']['search_type']
        return_res = DatamartSearchResult(search_result, supplied_data, query_json, search_type)
        if serialize_result.get('augmentation'):
            return_res.set_join_pairs([TabularJoinSpec.from_column_number_pairs(serialize_result['augmentation'])])
        return return_res


class TabularJoinSpec(AugmentSpec):
    """
    A join spec specifies a possible way to join a left dataset with a right dataset. The spec assumes that it may
    be necessary to use several columns in each datasets to produce a key or fingerprint that is useful for joining
    datasets. The spec consists of two lists of column identifiers or names (left_columns, left_column_names and
    right_columns, right_column_names).

    In the simplest case, both left and right are singleton lists, and the expectation is that an appropriate
    matching function exists to adequately join the datasets. In some cases equality may be an appropriate matching
    function, and in some cases fuzz matching is required. The join spec does not specify the matching function.

    In more complex cases, one or both left and right lists contain several elements. For example, the left list
    may contain columns for "city", "state" and "country" and the right dataset contains an "address" column. The join
    spec pairs up ["city", "state", "country"] with ["address"], but does not specify how the matching should be done
    e.g., combine the city/state/country columns into a single column, or split the address into several columns.
    """

    def __init__(self, left_columns: typing.List[typing.List[DatasetColumn]],
                 right_columns: typing.List[typing.List[DatasetColumn]],
                 left_resource_id: str = None, right_resource_id: str = None) -> None:

        self.left_resource_id = left_resource_id
        self.right_resource_id = right_resource_id
        self.left_columns = left_columns
        self.right_columns = right_columns
        if len(self.left_columns) != len(self.right_columns):
            shorter_len = min(len(self.right_columns), len(self.left_columns))
            self.left_columns = self.left_columns[:shorter_len]
            self.right_columns = self.right_columns[:shorter_len]
            print("The join spec length on left and right are different! Part of them will be ignored")

        # we can have list of the joining column pairs
        # each list inside left_columns/right_columns is a candidate joining column for that dataFrame
        # each candidate joining column can also have multiple columns

    def get_column_number_pairs(self):
        """
            A simple function used to get the pairs of column numbers only
            For example, it will return a join pair like ([1,2], [1])
        """
        all_pairs = []
        for each_left_col_pair, each_right_col_pair in zip(self.left_columns, self.right_columns):
            left = []
            right = []
            for each_left_col in each_left_col_pair:
                left.append(each_left_col.column_index)
            for each_right_col in each_right_col_pair:
                right.append(each_right_col.column_index)
            all_pairs.append((left, right))
        return all_pairs

    @classmethod
    def from_column_number_pairs(cls, col_number_pair: dict):
        """
        reconstruct the TabularJoinSpec base on the join pairs
        :param col_number_pair:
        :return:
        """
        left_columns = []
        right_columns = []
        left_pairs = col_number_pair['left_columns']
        right_pairs = col_number_pair['right_columns']
        if len(left_pairs) != len(right_pairs):
            raise ValueError("The given join pairs length on left = {} right = {} are different.".format(
                str(left_pairs), str(right_pairs)))

        for each_left_pair in left_pairs:
            left_pair_transferred = []
            for each_left_column in each_left_pair:
                left_pair_transferred.append(DatasetColumn(resource_id=None, column_index=each_left_column))
            left_columns.append(left_pair_transferred)
        for each_left_pair in right_pairs:
            right_pair_transferred = []
            for each_right_column in each_left_pair:
                right_pair_transferred.append(DatasetColumn(resource_id=None, column_index=each_right_column))
            right_columns.append(right_pair_transferred)
        return TabularJoinSpec(left_columns, right_columns)


class VariableConstraint(object):
    """
    Abstract class for all variable constraints.
    """

    def __init__(self, key: str, values: str):
        self.key = key
        self.values = values


class TemporalGranularity(utils.Enum):
    YEAR = 1
    MONTH = 2
    DAY = 3
    HOUR = 4
    SECOND = 5


class DatamartQuery:
    """
    A Datamart query consists of two parts:

    * A list of keywords.

    * A list of required variables. A required variable specifies that a matching dataset must contain a variable
      satisfying the constraints provided in the query. When multiple required variables are given, the matching
      dataset should contain variables that match each of the variable constraints.

    The matching is fuzzy. For example, when a user specifies a required variable spec using named entities, the
    expectation is that a matching dataset contains information about the given named entities. However, due to name,
    spelling, and other differences it is possible that the matching dataset does not contain information about all
    the specified entities.

    In general, Datamart will do a best effort to satisfy the constraints, but may return datasets that only partially
    satisfy the constraints.
    """

    def __init__(self, keywords: typing.List[str] = list(), variables: typing.List['VariableConstraint'] = list(),
                 search_type: str = "general", keywords_search: typing.List[str] = list(), title_search: str = "",
                 variables_search: dict = dict()) -> None:
        self.search_type = search_type
        self.keywords = keywords
        self.variables = variables
        self.keywords_search = keywords_search
        self.title_search = title_search
        self.variables_search = variables_search
