from datetime import datetime
from numbers import Number
import logging

from flask import jsonify
import werkzeug
from jsonschema import ValidationError

from . import query_cohd_mysql
from .cohd_utilities import omop_concept_curie
from .cohd_trapi import *
from .trapi import reasoner_validator_11x as reasoner_validator


class CohdTrapi110(CohdTrapi):
    """
    Pseudo-reasoner conforming to NCATS Biomedical Data Translator Reasoner API Spec 1.0
    """

    # Biolink categories that COHD TRAPI 1.1 supports (only the lowest level listed, not including ancestors)
    supported_categories = ['biolink:ChemicalSubstance', 'biolink:DiseaseOrPhenotypicFeature',
                            'biolink:Drug', 'biolink:Procedure']

    # Biolink predicates that COHD TRAPI 1.1 supports (only the lowest level listed, not including ancestors)
    supported_edge_types = ['biolink:correlated_with']

    def __init__(self, request):
        super().__init__(request)

        self._valid_query = False
        self._invalid_query_response = None
        self._json_data = None
        self._query_graph = None
        self._concept_1_qnode_key = None
        self._concept_2_qnode_key = None
        self._query_options = None
        self._method = None
        self._concept_1_omop_id = None
        self._concept_2_omop_id = None
        self._dataset_id = None
        self._domain_class_pairs = None
        self._threshold = None
        self._criteria = []
        self._min_cooccurrence = None
        self._confidence_interval = None
        self._concept_mapper = BiolinkConceptMapper()
        self._request = request
        self._max_results = CohdTrapi.default_max_results
        self._local_oxo = CohdTrapi.default_local_oxo
        self._kg_nodes = {}
        self._knowledge_graph = {
            'nodes': {},
            'edges': {}
        }
        self._cohd_results = []
        self._results = []
        self._logs = []
        self._log_level = CohdTrapi.default_log_level

        # Determine how the query should be performed
        self._interpret_query()

    def log(self, message: str, code: TrapiStatusCode = None, level=logging.DEBUG):
        # Add to TRAPI log if above desired log level
        if level >= self._log_level:
            level_str = {
                logging.DEBUG: 'DEBUG',
                logging.INFO: 'INFO',
                logging.WARNING: 'WARNING',
                logging.ERROR: 'ERROR'
            }
            self._logs.append({
                'timestamp': datetime.now().isoformat(),
                'level': level_str[level],
                'code': None if code is None else code.value,
                'message': message
            })

        # Also pass the message to the root logger
        logging.log(level=level, msg=message)

    def _check_query_input(self):
        """ Check that the input JSON data has the expected fields

        Parameters
        ----------
        json_data - JSON data from request

        Returns
        -------
        If successful: (True, None)
        If failed: (False, Response)

        """
        # Check the body contains the proper json request object
        if self._json_data is None:
            self._valid_query = False
            self._invalid_query_response = ('Missing JSON request body', 400)
            return self._valid_query, self._invalid_query_response

        # Use TRAPI Reasoner Validator to validate the query
        try:
            reasoner_validator.validate_Query(self._json_data)
            self.log('Query passed reasoner validator')
        except ValidationError as err:
            self._valid_query = False
            self._invalid_query_response = (str(err), 400)
            return self._valid_query, self._invalid_query_response

        # Check for the query_graph (nullable, but required for COHD)
        query_message = self._json_data.get('message')
        query_graph = query_message.get('query_graph')
        if query_graph is None or not query_graph:
            self._valid_query = False
            self._invalid_query_response = ('Unsupported query: query_graph missing from query_message or empty', 400)
            return self._valid_query, self._invalid_query_response

        # Check the structure of the query graph. Should have 2 nodes and 1 edge (one-hop query)
        nodes = query_graph.get('nodes')
        edges = query_graph.get('edges')
        if nodes is None or len(nodes) != 2 or edges is None or len(edges) != 1:
            self._valid_query = False
            self._invalid_query_response = ('Unsupported query. Only one-hop queries supported.', 400)
            return self._valid_query, self._invalid_query_response

        # Everything looks good so far
        self._valid_query = True
        self._invalid_query_response = None
        return True, None

    def _find_query_node(self, query_node_id):
        """ Finds the desired query node by node_id from the dict of query nodes in query_graph

        Parameters
        ----------
        query_nodes - List of nodes in query_graph
        query_node_id - node_id of desired node

        Returns
        -------
        query node object, or None if not found
        """
        assert self._query_graph is not None and query_node_id,\
            'cohd_translator.py::COHDTranslatorReasoner::_find_query_nodes() - empty query_graph or query_node_id'

        return self._query_graph['nodes'].get(query_node_id)

    @staticmethod
    def _process_qnode_category(categories: Union[str, Iterable]) -> List[str]:
        """ Process a qnode's categories in a query graph for COHD.
        1) converts a singular string to a list
        2) make sure format is correct (biolink prefix and snake case)
        3) suggest categories that COHD handles better

        Parameters
        ----------
        categories: str or list of str of categories

        Returns
        -------
        list of categories
        """
        # Fix any non-conforming category strings
        categories = [fix_blm_category(cat) for cat in categories]

        # Suggest changes to certain categories
        for i, category in enumerate(categories):
            preferred_category = suggest_blm_category(category)
            if preferred_category is not None and preferred_category not in categories:
                categories[i] = preferred_category

        return categories

    def _interpret_query(self):
        """ Interprets the JSON request data for how the query should be performed.

        Parameters
        ----------
        query_graph - The query graph (see Reasoner API Standard)

        Returns
        -------
        True if input is valid, otherwise (False, message)
        """
        try:
            self._json_data = self._request.get_json()
        except werkzeug.exceptions.BadRequest:
            self._valid_query = False
            self._invalid_query_response = ('Request body is not valid JSON', 400)
            return self._valid_query, self._invalid_query_response

        if self._json_data is None:
            self._valid_query = False
            self._invalid_query_response = ('Missing JSON payload', 400)
            return self._valid_query, self._invalid_query_response

        # Get the log level
        log_level = self._json_data.get('log_level')
        if log_level is not None:
            log_level_enum = {
                'DEBUG': logging.DEBUG,
                'INFO': logging.INFO,
                'WARNING': logging.WARNING,
                'ERROR': logging.ERROR
            }
            self._log_level = log_level_enum.get(log_level, CohdTrapi.default_log_level)

        # Check that the query input has the correct structure
        input_check = self._check_query_input()
        if not input_check[0]:
            return input_check

        # Get options that don't fit into query_graph structure from query_options
        self._query_options = self._json_data.get('query_options')
        if self._query_options is None:
            # No query options provided. Get default options for all query options (below)
            self._query_options = dict()

        # Get the query method and check that it matches a supported type
        self._method = self._query_options.get('method')
        if self._method is None or not self._method or not isinstance(self._method, str):
            self._method = CohdTrapi.default_method
            self._query_options['method'] = CohdTrapi.default_method
        else:
            if self._method not in CohdTrapi.supported_query_methods:
                self._valid_query = False
                self._invalid_query_response = ('Query method "{method}" not supported. Options are: {methods}'.format(
                    method=self._method, methods=','.join(CohdTrapi.supported_query_methods)), 400)
                return self._valid_query, self._invalid_query_response

        # Get the query_option for dataset ID
        self._dataset_id = self._query_options.get('dataset_id')
        if self._dataset_id is None or not self._dataset_id or not isinstance(self._dataset_id, Number):
            self._dataset_id = CohdTrapi.default_dataset_id
            self._query_options['dataset_id'] = CohdTrapi.default_dataset_id

        # Get the query_option for minimum co-occurrence
        self._min_cooccurrence = self._query_options.get('min_cooccurrence')
        if self._min_cooccurrence is None or not isinstance(self._min_cooccurrence, Number):
            self._min_cooccurrence = CohdTrapi.default_min_cooccurrence
            self._query_options['min_cooccurrence'] = CohdTrapi.default_min_cooccurrence

        # Get the query_option for confidence_interval. Only used for obsExpRatio. If not specified, use default.
        self._confidence_interval = self._query_options.get('confidence_interval')
        if self._confidence_interval is None or not isinstance(self._confidence_interval, Number) or \
                self._confidence_interval < 0 or self._confidence_interval >= 1:
            self._confidence_interval = CohdTrapi.default_confidence_interval
            self._query_options['confidence_interval'] = CohdTrapi.default_confidence_interval

        # Get the query_option for local_oxo
        self._local_oxo = self._query_options.get('local_oxo')
        if self._local_oxo is None or not isinstance(self._local_oxo, bool):
            self._local_oxo = CohdTrapi.default_local_oxo

        # Get the query_option for maximum mapping distance
        self._mapping_distance = self._query_options.get('mapping_distance')
        if self._mapping_distance is None or not isinstance(self._mapping_distance, Number):
            self._mapping_distance = CohdTrapi.default_mapping_distance

        # Get query_option for ontology_targets
        ontology_map = self._query_options.get('ontology_targets')
        if ontology_map and isinstance(ontology_map, dict):
            self._concept_mapper = BiolinkConceptMapper(ontology_map, distance=self._mapping_distance,
                                                        local_oxo=self._local_oxo)
        else:
            # Use default ontology map
            self._concept_mapper = BiolinkConceptMapper(distance=self._mapping_distance, local_oxo=self._local_oxo)

        # Get query_option for including only Biolink nodes
        self._biolink_only = self._query_options.get('biolink_only')
        if self._biolink_only is None or not isinstance(self._biolink_only, bool):
            self._biolink_only = CohdTrapi.default_biolink_only

        # Get query_option for the desired maximum number of results
        max_results = self._query_options.get('max_results')
        if max_results:
            # Don't allow user to specify more than default
            self._max_results = min(max_results, CohdTrapi.limit_max_results)

        # Get query information from query_graph
        self._query_graph = self._json_data['message']['query_graph']

        # Check that the query_graph is supported by the COHD reasoner (1-hop query)
        edges = self._query_graph['edges']
        if len(edges) != 1:
            self._valid_query = False
            self._invalid_query_response = ('COHD reasoner only supports 1-hop queries', 400)
            return self._valid_query, self._invalid_query_response

        # Check if the edge type is supported by COHD Reasoner
        self._query_edge_key = list(edges.keys())[0]  # Get first and only edge
        self._query_edge = edges[self._query_edge_key]
        edge_predicates = self._query_edge.get('predicates')
        if edge_predicates is not None:
            edge_supported = False
            for edge_predicate in edge_predicates:
                if not bm_toolkit.is_predicate(edge_predicate):
                    # Not a proper biolink predicate
                    self._valid_query = False
                    self._invalid_query_response = (f'{edge_predicate} was not recognized as a biolink predicate', 400)
                    return self._valid_query, self._invalid_query_response
                # Check if any of the predicates are an ancestor of biolink:correlated_with
                predicate_descendants = bm_toolkit.get_descendants(edge_predicate)
                for pd in predicate_descendants:
                    if bm_toolkit.get_element(pd).slot_uri in CohdTrapi110.supported_edge_types:
                        edge_supported = True
                        self._query_edge_predicates = edge_predicates
                        break
            if not edge_supported:
                self._valid_query = False
                self._invalid_query_response = \
                    (f'None of the predicates in {edge_predicates} are supported by COHD.', 400)
                return self._valid_query, self._invalid_query_response
        else:
            # TRAPI does not require predicates. If no predicates specified, find all relations
            self._query_edge_predicates = [CohdTrapi.default_predicate]

        # Get the QNodes
        # Note: qnode_key refers to the key identifier for the qnode in the QueryGraph's nodes property, e.g., "n00"
        subject_qnode_key = self._query_edge['subject']
        subject_qnode = self._find_query_node(subject_qnode_key)
        if subject_qnode is None:
            self._valid_query = False
            self._invalid_query_response = (f'QNode id "{subject_qnode_key}" not found in query graph', 400)
            return self._valid_query, self._invalid_query_response
        object_qnode_key = self._query_edge['object']
        object_qnode = self._find_query_node(object_qnode_key)
        if object_qnode is None:
            self._valid_query = False
            self._invalid_query_response = (f'QNode id "{object_qnode_key}" not found in query graph', 400)
            return self._valid_query, self._invalid_query_response

        # In COHD queries, concept_id_1 must be specified by ID. Figure out which QNode to use for concept_1
        node_ids = set()
        concept_1_qnode = None
        concept_2_qnode = None
        if 'ids' in subject_qnode:
            self._concept_1_qnode_key = subject_qnode_key
            concept_1_qnode = subject_qnode
            self._concept_2_qnode_key = object_qnode_key
            concept_2_qnode = object_qnode
            node_ids = node_ids.union(subject_qnode['ids'])
        if 'ids' in object_qnode:
            self._concept_1_qnode_key = object_qnode_key
            concept_1_qnode = object_qnode
            self._concept_2_qnode_key = subject_qnode_key
            concept_2_qnode = subject_qnode
            node_ids = node_ids.union(object_qnode['ids'])
        node_ids = list(node_ids)

        # COHD queries require at least 1 node with a specified ID
        if len(node_ids) == 0:
            self._valid_query = False
            self._invalid_query_response = ('COHD TRAPI requires at least one node to have an ID', 400)
            return self._valid_query, self._invalid_query_response

        # Find BLM - OMOP mappings for all identified query nodes
        node_mappings = self._concept_mapper.map_to_omop(node_ids)

        # Get concept_id_1. QNode IDs is a list. For now, just use the first ID that can map to OMOP
        # Todo: ID list needs to be handled properly
        found = False
        ids = concept_1_qnode['ids']
        for curie in ids:
            if node_mappings[curie] is not None:
                # Found an OMOP mapping. Use this CURIE
                self._concept_1_qnode_curie = curie
                self._concept_1_mapping = node_mappings[curie]
                self._concept_1_omop_id = self._concept_1_mapping['omop_concept_id']

                # Keep track of this mapping in the query_graph for the response
                self.log(f'{curie} mapped to  OMOP concept ID {self._concept_1_omop_id}', level=logging.INFO)
                concept_1_qnode['mapped_omop_concept'] = self._concept_1_mapping
                found = True
                break
        if not found:
            self._valid_query = False
            description = f'Could not map node {self._concept_1_qnode_key} to OMOP concept'
            self.log(f'Could not map node {self._concept_1_qnode_key} to OMOP concept',
                     code=TrapiStatusCode.COULD_NOT_MAP_CURIE_TO_LOCAL_KG, level=logging.WARNING)
            response = self._trapi_mini_response(TrapiStatusCode.COULD_NOT_MAP_CURIE_TO_LOCAL_KG, description)
            self._invalid_query_response = response, 200
            return self._valid_query, self._invalid_query_response

        # Get qnode categories and check the formatting
        self._concept_1_qnode_categories = concept_1_qnode.get('categories', None)
        if self._concept_1_qnode_categories is not None:
            self._concept_1_qnode_categories = CohdTrapi110._process_qnode_category(self._concept_1_qnode_categories)
            concept_1_qnode['categories'] = self._concept_1_qnode_categories
        self._concept_2_qnode_categories = concept_2_qnode.get('categories', None)
        if self._concept_2_qnode_categories is not None:
            self._concept_2_qnode_categories = CohdTrapi110._process_qnode_category(self._concept_2_qnode_categories)
            concept_2_qnode['categories'] = self._concept_2_qnode_categories

        # Get the desired association concept or category
        ids = concept_2_qnode.get('ids')
        if ids is not None and ids:
            # IDs were specified for the second QNode also
            # QNode IDs can be a list. For now, just use the first ID that can map to OMOP. Todo: handle list properly
            found = False
            for curie in ids:
                if node_mappings[curie] is not None:
                    # Found an OMOP mapping. Use this CURIE
                    self._concept_2_qnode_curie = curie
                    self._concept_2_mapping = node_mappings[curie]
                    self._concept_2_omop_id = self._concept_2_mapping['omop_concept_id']

                    # Keep track of this mapping in the query_graph for the response
                    self.log(f'{curie} mapped to  OMOP concept ID {self._concept_2_omop_id}', level=logging.INFO)
                    concept_2_qnode['mapped_omop_concept'] = self._concept_2_mapping
                    found = True

                    # If CURIE of the 2nd node is specified, then query the association between concept_1 and concept_2
                    self._domain_class_pairs = None
                    break
            if not found:
                self._valid_query = False
                description = f'Could not map node {self._concept_2_qnode_key} to OMOP concept'
                response = self._trapi_mini_response(TrapiStatusCode.COULD_NOT_MAP_CURIE_TO_LOCAL_KG, description)
                self._invalid_query_response = response, 200
                return self._valid_query, self._invalid_query_response
        elif self._concept_2_qnode_categories is not None and self._concept_2_qnode_categories:
            # If CURIE is not specified and target node's category is specified, then query the association
            # between concept_1 and all concepts in the domain
            self._concept_2_qnode_curie = None
            self._concept_2_omop_id = None

            # If biolink:NamedThing is in the list of categories, query for associations against all concepts
            if 'biolink:NamedThing' in self._concept_2_qnode_categories:
                self._concept_2_qnode_curie = None
                self._concept_2_omop_id = None
                self._domain_class_pairs = None
                self.log(f'Querying associations to all OMOP domains', level=logging.INFO)
            else:
                self._domain_class_pairs = set()
                # Check if any of the categories supported by COHD are included in the categories list (or one of their
                # descendants)
                for supported_cat in CohdTrapi110.supported_categories:
                    supported_cat_name = bm_toolkit.get_element(supported_cat).name
                    for queried_cat in self._concept_2_qnode_categories:
                        if supported_cat_name in bm_toolkit.get_descendants(queried_cat):
                            dc_pair = map_blm_class_to_omop_domain(supported_cat)
                            self._domain_class_pairs = self._domain_class_pairs.union(dc_pair)

                if self._domain_class_pairs is None or len(self._domain_class_pairs) == 0:
                    # None of the categories for this QNode were mapped to OMOP
                    self._valid_query = False
                    description = f"None of QNode {self._concept_2_qnode_key}'s categories " \
                                  f"({self._concept_2_qnode_categories}) are supported by COHD"
                    response = self._trapi_mini_response(TrapiStatusCode.UNSUPPORTED_QNODE_CATEGORY, description)
                    self._invalid_query_response = response, 200
                    return self._valid_query, self._invalid_query_response

                for dc_pair in self._domain_class_pairs:
                    if dc_pair.concept_class_id is not None:
                        self.log(f'Querying associations to all OMOP domain {dc_pair.domain_id}:' \
                                 f'{dc_pair.concept_class_id}', level=logging.INFO)
                    else:
                        self.log(f'Querying associations to all OMOP domain {dc_pair.domain_id}', level=logging.INFO)
        else:
            # No CURIE or type specified, query for associations against all concepts
            self._concept_2_qnode_curie = None
            self._concept_2_omop_id = None
            self._domain_class_pairs = None

        if 'contraints' in concept_1_qnode:
            self._valid_query = False
            self._invalid_query_response = 'COHD does not support constraints on a QNode with ids specified', 400
            return self._valid_query, self._invalid_query_response

        qnode2_constraints = concept_2_qnode.get('constraints', None)
        if qnode2_constraints is not None and len(qnode2_constraints) > 0:
            # COHD does not yet support constraints
            self._valid_query = False
            self._invalid_query_response = 'COHD has not yet implemented support of constraints', 400
            return self._valid_query, self._invalid_query_response

        # Criteria for returning results
        self._criteria = []

        # Add a criterion for minimum co-occurrence
        if self._min_cooccurrence > 0:
            self._criteria.append(ResultCriteria(function=criteria_min_cooccurrence,
                                                 kargs={'cooccurrence': self._min_cooccurrence}))

        # Get query_option for threshold. Don't use filter if not specified (i.e., no default option for threshold)
        self._threshold = self._query_options.get('threshold')
        if self._threshold is not None and isinstance(self._threshold, Number):
            self._criteria.append(ResultCriteria(function=criteria_threshold,
                                                 kargs={'threshold': self._threshold}))

        # If the method is obsExpRatio, add a criteria for confidence interval
        if self._method.lower() == 'obsexpratio' and self._confidence_interval > 0:
            self._criteria.append(ResultCriteria(function=criteria_confidence,
                                                 kargs={'confidence': self._confidence_interval}))

        if self._valid_query:
            return True
        else:
            return self._valid_query, self._invalid_query_response

    def operate(self):
        """ Performs the COHD query and reasoning.

        Returns
        -------
        Response message with JSON data in Translator Reasoner API Standard
        """
        # Check if the query is valid
        if self._valid_query:
            self._cohd_results = []
            if self._concept_2_omop_id is None and self._domain_class_pairs:
                # Node 2 not specified, but node 2's type was specified. Query associations between Node 1 and the
                # requested types (domains)
                for domain_id, concept_class_id in self._domain_class_pairs:
                    new_results = query_cohd_mysql.query_association(method=self._method,
                                                                     concept_id_1=self._concept_1_omop_id,
                                                                     concept_id_2=self._concept_2_omop_id,
                                                                     dataset_id=self._dataset_id,
                                                                     domain_id=domain_id,
                                                                     concept_class_id=concept_class_id,
                                                                     confidence=self._confidence_interval)
                    if new_results:
                        self._cohd_results.extend(new_results['results'])
            else:
                # Either Node 2 was specified by a CURIE or no type (domain) was specified for type 2. Query the
                # associations between Node 1 and Node 2 or between Node 1 and all domains
                json_results = query_cohd_mysql.query_association(method=self._method,
                                                                  concept_id_1=self._concept_1_omop_id,
                                                                  concept_id_2=self._concept_2_omop_id,
                                                                  dataset_id=self._dataset_id,
                                                                  domain_id=None,
                                                                  confidence=self._confidence_interval)
                self._cohd_results = json_results['results']

            # Results within each query call should be sorted, but still need to be sorted across query calls
            self._cohd_results = sort_cohd_results(self._cohd_results)

            # Convert results from COHD format to Translator Reasoner standard
            return self._serialize_trapi_response()
        else:
            # Invalid query. Return the invalid query response
            return self._invalid_query_response

    def _add_cohd_result(self, cohd_result, criteria):
        """ Adds a COHD result. The COHD result is always added to the knowledge graph. If the COHD result passes all
        criteria, it is also added to the results.

        Parameters
        ----------
        cohd_result
        criteria: List - [ResultCriteria]
        """
        assert cohd_result is not None and 'concept_id_1' in cohd_result and 'concept_id_2' in cohd_result, \
            'Translator::KnowledgeGraph::add_edge() - Bad cohd_result'

        # Check if result passes all filters before adding
        if criteria is not None:
            if not all([c.check(cohd_result) for c in criteria]):
                return

        # Get node for concept 1
        concept_1_id = cohd_result['concept_id_1']
        node_1 = self._get_kg_node(concept_1_id, query_node_curie=self._concept_1_qnode_curie,
                                   query_node_categories=self._concept_1_qnode_categories)

        if self._biolink_only and not node_1.get('biolink_compliant', False):
            # Only include results when node_1 maps to biolink
            return

        # Get node for concept 2
        concept_2_id = cohd_result['concept_id_2']
        concept_2_name = cohd_result.get('concept_2_name')
        concept_2_domain = cohd_result.get('concept_2_domain')
        node_2 = self._get_kg_node(concept_2_id, concept_2_name, concept_2_domain,
                                   query_node_curie=self._concept_2_qnode_curie,
                                   query_node_categories=self._concept_2_qnode_categories)

        if self._biolink_only and not node_2.get('biolink_compliant', False):
            # Only include results when node_2 maps to biolink
            return

        # Add nodes and edge to knowledge graph
        if self._query_edge['subject'] == self._concept_1_qnode_key:
            subject_node = node_1
            object_node = node_2
        elif self._query_edge['subject'] == self._concept_2_qnode_key:
            subject_node = node_2
            object_node = node_1
        else:
            raise LookupError("Error mapping query nodes to edges")
        kg_node_1, kg_node_2, kg_edge, kg_edge_id = self._add_kg_edge(subject_node, object_node, cohd_result)

        # Add to results
        self._add_result(node_1['primary_curie'], node_2['primary_curie'], kg_edge_id)

    def _add_result(self, kg_node_1_id, kg_node_2_id, kg_edge_id):
        """ Adds a knowledge graph edge to the results list

        Parameters
        ----------
        kg_node_1_id: Subject node ID
        kg_node_2_id: Object node ID
        kg_edge_id: edge ID

        Returns
        -------
        result
        """
        result = {
            'node_bindings': {
                self._concept_1_qnode_key: [{
                    'id': kg_node_1_id
                }],
                self._concept_2_qnode_key: [{
                    'id': kg_node_2_id
                }]
            },
            'edge_bindings': {
                self._query_edge_key: [{
                    'id': kg_edge_id
                }]
            }
        }
        self._results.append(result)
        return result

    def _get_kg_node(self, concept_id, concept_name=None, domain=None, concept_class=None, query_node_curie=None,
                     query_node_categories=None):
        """ Gets the node from internal "graph" representing the OMOP concept. Creates the node if not yet created.
        Node is not added to the knowledge graph or results.

        Parameters
        ----------
        concept_id: OMOP concept ID
        concept_name: OMOP concept name
        domain: OMOP concept domain
        concept_class: OMOP concept class ID
        query_node_curie: CURIE used in the QNode corresponding to this KG Node
        query_node_categories: list of categories for this QNode

        Returns
        -------
        Node for internal use
        """
        node = self._kg_nodes.get(concept_id)

        if node is None:
            # Create the node
            if concept_name is None or domain is None or concept_class is None:
                # Concept information not specified, lookup concept definition
                concept_name = concept_name if concept_name is not None else ''
                domain = domain if domain is not None else ''
                concept_def = query_cohd_mysql.omop_concept_definition(concept_id)

                if concept_def is not None:
                    if not concept_name:
                        concept_name = concept_def['concept_name']
                    if not domain:
                        domain = concept_def['domain_id']
                    if not concept_class:
                        concept_class = concept_def['concept_class_id']

            # If we don't find better mappings (below) for this concept, default to OMOP CURIE and label
            omop_curie = omop_concept_curie(concept_id)
            primary_curie = omop_curie
            primary_label = concept_name

            # Map to Biolink Model or other target ontologies
            blm_category = map_omop_domain_to_blm_class(domain, concept_class, query_node_categories)
            mapped_to_blm = False
            mapping = None
            if self._concept_mapper:
                mapping = self._concept_mapper.map_from_omop(concept_id, blm_category)

            if query_node_curie is not None and query_node_curie:
                # The CURIE was specified for this node in the query_graph, use that CURIE to identify this node
                mapped_to_blm = True
                primary_curie = query_node_curie
                # Find the label from the mappings
                if mapping is not None:
                    primary_label = mapping['target_label']
            elif mapping is not None:
                # Choose one of the mappings to be the main identifier for the node. Prioritize distance first, and then
                # choose by the order of prefixes listed in the Concept Mapper. If no biolink prefix found, use OMOP
                primary_curie = mapping['target_curie']
                primary_label = mapping['target_label']
                mapped_to_blm = True

            # Create representations for the knowledge graph node and query node, but don't add them to the graphs yet
            internal_id = '{id:06d}'.format(id=len(self._kg_nodes))
            node = {
                'omop_id': concept_id,
                'name': concept_name,
                'domain': domain,
                'internal_id': internal_id,
                'primary_curie': primary_curie,
                'in_kgraph': False,
                'biolink_compliant': mapped_to_blm,
                'kg_node': {
                    'name': primary_label,
                    'categories': [blm_category],
                    'attributes': [
                        {
                            'attribute_type_id': 'EDAM:data_1087',  # Ontology concept ID
                            'original_attribute_name': 'concept_id',
                            'value': omop_curie,
                            'value_type_id': 'EDAM:data_1087',  # Ontology concept ID
                            'attribute_source': 'OMOP',
                            'value_url': f'https://athena.ohdsi.org/search-terms/terms/{concept_id}'
                        },
                        {
                            'attribute_type_id': 'EDAM:data_2339',  # Ontology concept name
                            'original_attribute_name': 'concept_name',
                            'value': concept_name,
                            'value_type_id': 'EDAM:data_2339',  # Ontology concept name
                            'attribute_source': 'OMOP',
                        },
                        {
                            'attribute_type_id': 'EDAM:data_0967',  # Ontology concept data
                            'original_attribute_name': 'domain',
                            'value': domain,
                            'value_type_id': 'EDAM:data_0967',  # Ontology concept data
                            'attribute_source': 'OMOP',
                        }
                    ]
                }
            }
            self._kg_nodes[concept_id] = node

        return node

    def _add_kg_node(self, node):
        """ Adds the node to the knowledge graph

        Parameters
        ----------
        node: Node

        Returns
        -------
        node
        """
        kg_node = node['kg_node']
        if not node['in_kgraph']:
            self._knowledge_graph['nodes'][node['primary_curie']] = kg_node
            node['in_kgraph'] = True

        return kg_node

    def _add_kg_edge(self, node_1, node_2, cohd_result):
        """ Adds the edge to the knowledge graph

        Parameters
        ----------
        node_1: Subject node
        node_2: Object node
        cohd_result: COHD result - data gets added to edge

        Returns
        -------
        kg_node_1, kg_node_2, kg_edge
        """
        # Add nodes to knowledge graph
        kg_node_1 = self._add_kg_node(node_1)
        kg_node_2 = self._add_kg_node(node_2)

        # Mint a new identifier
        ke_id = 'ke{id:06d}'.format(id=len(self._knowledge_graph['edges']))

        # Add properties from COHD results to the edge attributes
        attributes = list()
        if 'p-value' in cohd_result:
            attributes.append({
                'attribute_type_id': 'biolink:p_value',
                'original_attribute_name': 'p-value',
                'value': cohd_result['p-value'],
                'value_type_id': 'EDAM:data_1669',  # P-value
                'attribute_source': 'COHD',
                'value_url': 'http://edamontology.org/data_1669'
            })
        if 'confidence_interval' in cohd_result:
            attributes.append({
                'attribute_type_id': 'biolink:has_confidence_level',
                'original_attribute_name': 'confidence_interval',
                'value': cohd_result['confidence_interval'],
                'value_type_id': 'EDAM:data_0951',  # Statistical estimate score
                'attribute_source': 'COHD'
            })
        if 'dataset_id' in cohd_result:
            attributes.append({
                'attribute_type_id': 'biolink:provided_by',  # Database ID
                'original_attribute_name': 'dataset_id',
                'value': cohd_result['dataset_id'],
                'value_type_id': 'EDAM:data_1048',  # Database ID
                'attribute_source': 'COHD'
            })
        if 'expected_count' in cohd_result:
            attributes.append({
                'attribute_type_id': 'EDAM:operation_3438',
                'original_attribute_name': 'expected_count',
                'value': cohd_result['expected_count'],
                'value_type_id': 'EDAM:operation_3438',  # Calculation (not sure if it's correct to use an operation)
                'attribute_source': 'COHD'
            })
        # Some properties are handled together as a group based on their type
        for key in ['ln_ratio', 'relative_frequency']:
            if key in cohd_result:
                attributes.append({
                    'attribute_type_id': 'biolink:has_evidence',
                    'original_attribute_name': key,
                    'value': cohd_result[key],
                    'value_type_id': 'EDAM:data_1772',  # Score
                    'attribute_source': 'COHD'
                })
        for key in ['observed_count',  # From ln_ratio
                    'concept_pair_count', 'concept_2_count',  # From relative_frequency
                    'n', 'n_c1', 'n_c1_c2', 'n_c1_~c2', 'n_c2', 'n_~c1_c2', 'n_~c1_~c2'  # From chi_square
                    ]:
            if key in cohd_result:
                attributes.append({
                    'attribute_type_id': 'biolink:has_count',
                    'original_attribute_name': key,
                    'value': cohd_result[key],
                    'value_type_id': 'EDAM:data_0006',  # Data
                    'attribute_source': 'COHD'
                })

        # Set the knowledge graph edge properties
        kg_edge = {
            'predicate': self._get_kg_predicate(),
            'subject': node_1['primary_curie'],
            'object': node_2['primary_curie'],
            'attributes': attributes
        }

        # Add the new edge
        self._knowledge_graph['edges'][ke_id] = kg_edge

        return kg_node_1, kg_node_2, kg_edge, ke_id

    def _serialize_trapi_response(self,
                                  status: TrapiStatusCode = TrapiStatusCode.SUCCESS):
        """ Creates the response message with JSON data in Reasoner Std API format

        Returns
        -------
        Response message with JSON data in Reasoner Std API format
        """
        if self._cohd_results is not None:
            for result in self._cohd_results:
                self._add_cohd_result(result, self._criteria)

                # Don't add more than the maximum number of results
                if len(self._results) >= self._max_results:
                    break

        if len(self._results) == 0:
            status = TrapiStatusCode.NO_RESULTS

        response = {
            'status': status.value,
            'description': f'COHD returned {len(self._results)} results.',
            'message': {
                'results': self._results,
                'query_graph': self._query_graph,
                'knowledge_graph': self._knowledge_graph
            },
            # From TRAPI Extended
            'reasoner_id': 'COHD',
            'tool_version': 'COHD 3.0.0',
            'schema_version': '1.0.0',
            'datetime': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'query_options': self._query_options,
        }
        if self._logs is not None and self._logs:
            response['logs'] = self._logs
        return jsonify(response)

    def _trapi_mini_response(self,
                             status: TrapiStatusCode,
                             description: str):
        """ Creates a minimal TRAPI response without creating the knowledge graph or results.
        This is useful for situations where some issue occurred but the TRAPI convention expects an HTTP
        Status Code 200 and TRAPI Response object.

        Returns
        -------
        Response message with JSON data in Reasoner Std API format
        """
        response = {
            'status': status.value,
            'description': description,
            'message': {
                'results': None,
                'query_graph': self._query_graph,
                'knowledge_graph': None
            },
            # From TRAPI Extended
            'reasoner_id': 'COHD',
            'tool_version': 'COHD 3.0.0',
            'schema_version': '1.0.0',
            'datetime': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'query_options': self._query_options,
        }
        if self._logs is not None and self._logs:
            response['logs'] = self._logs
        return jsonify(response)