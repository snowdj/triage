import importlib
import logging
from datetime import datetime
from textwrap import dedent

from sklearn.model_selection import ParameterGrid

from triage.component import architect
from triage.component import catwalk
from triage.component.timechop import Timechop

from triage.util.conf import convert_str_to_relativedelta
from triage.validation_primitives import (
    table_should_have_data,
    column_should_be_intlike,
    column_should_be_stringlike,
    column_should_be_timelike,
)


class Validator(object):

    def __init__(self, db_engine=None, strict=True):
        self.db_engine = db_engine
        self.strict = strict

    def run(self, *args, **kwargs):
        try:
            self._run(*args, **kwargs)
        except ValueError as e:
            if self.strict:
                raise ValueError(e)
            else:
                logging.warning('Validation error hit, not running in strict mode so continuing on: %s', str(e))

class TemporalValidator(Validator):

    def _run(self, temporal_config):
        def dt_from_str(dt_str):
            return datetime.strptime(dt_str, '%Y-%m-%d')
        splits = []
        try:
            chopper = Timechop(
                feature_start_time=dt_from_str(
                    temporal_config['feature_start_time']),
                feature_end_time=dt_from_str(
                    temporal_config['feature_end_time']),
                label_start_time=dt_from_str(
                    temporal_config['label_start_time']),
                label_end_time=dt_from_str(temporal_config['label_end_time']),
                model_update_frequency=temporal_config['model_update_frequency'],
                training_label_timespans=temporal_config['training_label_timespans'],
                test_label_timespans=temporal_config['test_label_timespans'],
                training_as_of_date_frequencies=temporal_config['training_as_of_date_frequencies'],
                test_as_of_date_frequencies=temporal_config['test_as_of_date_frequencies'],
                max_training_histories=temporal_config['max_training_histories'],
                test_durations=temporal_config['test_durations'],
            )
            splits = chopper.chop_time()
        except Exception as e:
            raise ValueError(dedent('''
            Section: temporal_config -
            Timechop could not produce temporal splits from config {}.
            Error: {}
            '''.format(temporal_config, e)))
        for split_num, split in enumerate(splits):
            if len(split['train_matrix']['as_of_times']) == 0:
                raise ValueError(dedent('''
                Section: temporal_config -
                Computed split {} has a train matrix with no as_of_times.
                '''.format(split)))

            # timechop computes the last time available to train data
            # and stores it in the matrix as 'matrix_info_end_time'
            # but to be more sure, let's double-check by comparing as_of_times
            # in the train and all associated test matrices
            train_max_data_time = max(split['train_matrix']['as_of_times']) +\
                convert_str_to_relativedelta(
                    split['train_matrix']['training_label_timespan'])

            for test_matrix in split['test_matrices']:
                if len(test_matrix['as_of_times']) == 0:
                    raise ValueError(dedent('''
                    Section: temporal_config -
                    Computed split {} has a test matrix with no as_of_times.
                    '''.format(split)))
                overlapping_times = [
                    as_of_time
                    for as_of_time in test_matrix['as_of_times']
                    if as_of_time < train_max_data_time
                ]
                if overlapping_times:
                    raise ValueError(dedent('''
                    Section: temporal_config -
                    Computed split index {} has a test matrix with as_of_times {}
                    < the maximum train as_of_time + train label timespan.
                    ({}). This is likely an error in timechop. See the
                    experiment's split_definitions[{}] for more information'''.format(
                        split_num,
                        overlapping_times,
                        train_max_data_time,
                        split_num
                    )))


class FeatureAggregationsValidator(Validator):
    def _validate_keys(self, aggregation_config):
        for key in [
            'from_obj',
            'intervals',
            'groups',
            'knowledge_date_column',
            'prefix'
        ]:
            if key not in aggregation_config:
                raise ValueError(dedent('''
                Section: feature_aggregations -
                '{} required as key: aggregation config: {}'''.format(key, aggregation_config)))

    def _validate_aggregates(self, aggregation_config):
        if 'aggregates' not in aggregation_config \
                and 'categoricals' not in aggregation_config \
                and 'array_categoricals' not in aggregation_config:
            raise ValueError(dedent('''
            Section: feature_aggregations -
            Need either aggregates, categoricals, or array_categoricals
            in {}'''.format(aggregation_config)))

    def _validate_categoricals(self, categoricals):
        conn = self.db_engine.connect()
        for categorical in categoricals:
            if 'choice_query' in categorical and 'choices' in categorical:
                raise ValueError(dedent('''
                Section: feature_aggregations -
                Both 'choice_query' and 'choices' specified for {}.
                Please only specify one.'''.format(categorical)))
            if not ('choice_query' in categorical or 'choices' in categorical):
                raise ValueError(dedent('''
                Section: feature_aggregations -
                Neither 'choice_query' and 'choices' specified for {}.
                Please specify one.'''.format(categorical)))
            if 'choice_query' in categorical:
                logging.info('Validating choice query')
                choice_query = categorical['choice_query']
                try:
                    conn.execute('explain {}'.format(choice_query))
                except Exception as e:
                    raise ValueError(dedent('''
                    Section: feature_aggregations -
                    choice query does not run.
                    choice query: "{}"
                    Full error: {}'''.format(choice_query, e)))

    def _validate_from_obj(self, from_obj):
        conn = self.db_engine.connect()
        logging.info('Validating from_obj')
        try:
            conn.execute('explain select * from {}'.format(from_obj))
        except Exception as e:
            raise ValueError(dedent('''
                Section: feature_aggregations -
                from_obj query does not run.
                from_obj: "{}"
                Full error: {}'''.format(from_obj, e)))

    def _validate_time_intervals(self, intervals):
        logging.info('Validating time intervals')
        for interval in intervals:
            if interval != 'all':
                # this function, used elsewhere to break up time intervals,
                # will throw an error if the interval can't be converted to a
                # relativedelta
                try:
                    convert_str_to_relativedelta(interval)
                except Exception as e:
                    raise ValueError(dedent('''
                    Section: feature_aggregations -
                    Time interval is invalid.
                    interval: "{}"
                    Full error: {}'''.format(interval, e)))

    def _validate_groups(self, groups):
        if 'entity_id' not in groups:
            raise ValueError(dedent('''
            Section: feature_aggregations -
            List of groups needs to include 'entity_id'.
            Passed list: {}'''.format(groups)))

    def _validate_imputation_rule(self, aggregate_type, impute_rule):
        """Validate the imputation rule for a given aggregation type."""
        # dictionary of imputation type : required parameters
        valid_imputations = {
            'all': {
                'mean': [],
                'constant': ['value'],
                'zero': [],
                'zero_noflag': [],
                'error': []
            },
            'aggregates': {
                'binary_mode': [],
            },
            'categoricals': {
                'null_category': []
            }
        }
        valid_imputations['array_categoricals'] = valid_imputations['categoricals']

        # the valid imputation rules for the specific aggregation type being checked
        valid_types = dict(
            valid_imputations['all'], **valid_imputations[aggregate_type])

        # no imputation rule was specified
        if 'type' not in impute_rule.keys():
            raise ValueError(dedent('''
            Section: feature_aggregations -
            Imputation type must be specified'''))

        # a rule was specified, but not valid for this type of aggregate
        if impute_rule['type'] not in valid_types.keys():
            raise ValueError(dedent('''
            Section: feature_aggregations -
            Invalid imputation type %s for %s''' % (impute_rule['type'], aggregate_type)))

        # check that all required parameters exist in the keys of the imputation rule
        required_params = valid_types[impute_rule['type']]
        for param in required_params:
            if param not in impute_rule.keys():
                raise ValueError(dedent('''
                Section: feature_aggregations -
                Missing param %s for %s''' % (param, impute_rule['type'])))

    def _validate_imputations(self, aggregation_config):
        """Validate the imputation rules in an aggregation config, looping
        through all three types of aggregates. Most of the work here is
        done by _validate_imputation_rule() to check the requirements of
        each imputation rule found
        """
        agg_types = ['aggregates', 'categoricals', 'array_categoricals']

        for agg_type in agg_types:
            # base_imp are the top-level rules, `such as aggregates_imputation`
            base_imp = aggregation_config.get(agg_type+'_imputation', {})

            # loop through the individual aggregates
            for agg in aggregation_config.get(agg_type, []):
                # combine any aggregate-level imputation rules with top-level ones
                imp_dict = dict(base_imp, **agg.get('imputation', {}))

                # imputation rules are metric-specific, so check each metric's rule
                for metric in agg['metrics']:
                    # metric rules may be defined by the metric name (e.g., 'max')
                    # or with the 'all' catch-all, with named metrics taking
                    # precedence. If we fall back to {}, the rule validator will
                    # error out on no metric found.
                    impute_rule = imp_dict.get(metric, imp_dict.get('all', {}))
                    self._validate_imputation_rule(agg_type, impute_rule)

    def _validate_aggregation(self, aggregation_config):
        logging.info('Validating aggregation config %s', aggregation_config)
        self._validate_keys(aggregation_config)
        self._validate_aggregates(aggregation_config)
        self._validate_categoricals(aggregation_config.get('categoricals', []))
        self._validate_from_obj(aggregation_config['from_obj'])
        self._validate_time_intervals(aggregation_config['intervals'])
        self._validate_groups(aggregation_config['groups'])
        self._validate_imputations(aggregation_config)

    def _run(self, feature_aggregation_config):
        """Validate a feature aggregation config applied to this object

        The validations range from basic type checks, key presence checks,
        as well as validating the sql in from objects.

        Args:
            feature_aggregation_config (list) all values, except for feature
                date, necessary to instantiate a collate.SpacetimeAggregation

        Raises: ValueError if any part of the config is found to be invalid
        """
        if not feature_aggregation_config:
            raise ValueError(dedent('''
            Section: feature_aggregations -
            Section not found. You must define feature aggregations.'''))
        for aggregation in feature_aggregation_config:
            self._validate_aggregation(aggregation)


class LabelConfigValidator(Validator):
    def _validate_query(self, query):
        if '{as_of_date}' not in query:
            raise ValueError(dedent('''
            Section: label_config -
            If 'query' is used as label_config,
            {as_of_date} must be present'''))
        if '{label_timespan}' not in query:
            raise ValueError(dedent('''
            Section: label_config -
            If 'query' is used as label_config,
            {label_timespan} must be present'''))
        bound_query = query\
            .replace('{as_of_date}', '2016-01-01')\
            .replace('{label_timespan}', '6month')
        conn = self.db_engine.connect()
        logging.info('Validating label query via EXPLAIN')
        try:
            conn.execute('explain {}'.format(bound_query))
        except Exception as e:
            raise ValueError(dedent('''
                Section: label_config -
                given query can not be run with a sample as_of_date and label_timespan.
                query: "{}"
                Full error: {}'''.format(query, e)))

    @staticmethod
    def _validate_include_missing_labels_in_train_as(missing_label_flag):
        if missing_label_flag not in {None, True, False}:
            raise ValueError(dedent('''
            Section: label_config -
            The value for 'include_missing_labels_in_train_as', {}, is invalid.
            The key must be either absent, or a boolean value True or False
            Triage only supports binary labels at this time.'''.format(missing_label_flag)))

    def _run(self, label_config):
        if not label_config:
            raise ValueError(dedent('''
            Section: label_config -
            Section not found. You must define a label config.'''))

        if 'query' not in label_config:
            raise ValueError(dedent('''
            Section: label_config -
            key 'query' not found. You must define a label query.'''))
        self._validate_query(label_config['query'])
        self._validate_include_missing_labels_in_train_as(
            label_config.get('include_missing_labels_in_train_as', None)
        )


class CohortConfigValidator(Validator):
    def _run(self, cohort_config):
        mutex_keys = set(['dense_states', 'entities_table', 'query'])
        available_keys = mutex_keys | set(['name'])
        used_keys = set(cohort_config.keys())
        bad_keys = used_keys - available_keys
        if bad_keys:
            raise ValueError(dedent('''
                Section: cohort_config -
                The following given keys: '{}'
                are invalid. Available keys are: '{}'
                '''.format(bad_keys, available_keys)))
        used_mutex_keys = mutex_keys & used_keys
        if len(used_mutex_keys) > 1:
            raise ValueError(dedent('''
                Section: cohort_config -
                Only one of the following keys can be sent: '{}'
                Found '{}'
                '''.format(mutex_keys, used_mutex_keys)))

        if 'dense_states' in cohort_config:
            state_config = cohort_config['dense_states']
            if 'table_name' not in state_config:
                raise ValueError(dedent('''
                Section: cohort_config -
                If 'dense_states' is used as cohort config,
                a table name must be present'''))
            dense_state_table = state_config['table_name']
            table_should_have_data(dense_state_table, self.db_engine)
            column_should_be_intlike(
                dense_state_table, 'entity_id', self.db_engine)
            column_should_be_stringlike(
                dense_state_table, 'state', self.db_engine)
            column_should_be_timelike(
                dense_state_table, 'start_time', self.db_engine)
            column_should_be_timelike(
                dense_state_table, 'end_time', self.db_engine)
            if 'state_filters' not in state_config or len(state_config['state_filters']) < 1:
                raise ValueError(dedent('''
                Section: cohort_config -
                If 'dense_states' is used as cohort config,
                at least one state filter must be present'''))
        elif 'entities_table' in cohort_config:
            entities_table = cohort_config['entities_table']
            table_should_have_data(entities_table, self.db_engine)
            column_should_be_intlike(
                entities_table, 'entity_id', self.db_engine)
        elif 'query' in cohort_config:
            query = cohort_config['query']
            if '{as_of_date}' not in query:
                raise ValueError(dedent('''
                Section: cohort_config -
                If 'query' is used as cohort_config,
                {as_of_date} must be present'''))
            dated_query = query.replace(
                '{as_of_date}', '2016-01-01')
            conn = self.db_engine.connect()
            logging.info('Validating cohort query')
            try:
                conn.execute(f'explain {dated_query}')
            except Exception as e:
                raise ValueError(dedent(f'''
                    Section: cohort_config -
                    given query can not be run with a sample as_of_date .
                    query: "{query}"
                    Full error: {e}'''))


class FeatureGroupDefinitionValidator(Validator):
    def _run(self, feature_group_definition, feature_aggregation_config):
        if not isinstance(feature_group_definition, dict):
            raise ValueError(dedent('''
            Section: feature_group_definition -
            feature_group_definition must be a dictionary'''))

        available_subsetters = architect.feature_group_creator.FeatureGroupCreator.subsetters
        for subsetter_name, value in feature_group_definition.items():
            if subsetter_name not in available_subsetters:
                raise ValueError(dedent('''
                Section: feature_group_definition -
                Unknown feature_group_definition key {} received.
                Available keys are {}'''.format(subsetter_name, available_subsetters)))
            if not hasattr(value, '__iter__') or isinstance(value, (str, bytes)):
                raise ValueError(dedent('''
                Section: feature_group_definition -
                feature_group_definition value for {}, {}
                should be a list'''.format(subsetter_name, value)))

        if 'prefix' in feature_group_definition:
            available_prefixes = {
                aggregation['prefix']
                for aggregation in feature_aggregation_config
            }
            bad_prefixes = set(
                feature_group_definition['prefix']) - available_prefixes
            if bad_prefixes:
                raise ValueError(dedent('''
                Section: feature_group_definition -
                The following given feature group prefixes: '{}'
                are invalid. Available prefixes from this experiment's feature
                aggregations are: '{}'
                '''.format(bad_prefixes, available_prefixes)))

        if 'tables' in feature_group_definition:
            available_tables = {
                aggregation['prefix'] + '_aggregation_imputed'
                for aggregation in feature_aggregation_config
            }
            bad_tables = set(
                feature_group_definition['tables']) - available_tables
            if bad_tables:
                raise ValueError(dedent('''
                Section: feature_group_definition -
                The following given feature group tables: '{}'
                are invalid. Available tables from this experiment's feature
                aggregations are: '{}'
                '''.format(bad_tables, available_tables)))


class FeatureGroupStrategyValidator(Validator):
    def _run(self, feature_group_strategies):
        if not isinstance(feature_group_strategies, list):
            raise ValueError(dedent('''
            Section: feature_group_strategies -
            feature_group_strategies section must be a list'''))
        available_strategies = {
            key for key in
            architect.feature_group_mixer.FeatureGroupMixer.strategy_lookup.keys()
        }
        bad_strategies = set(feature_group_strategies) - available_strategies
        if bad_strategies:
            raise ValueError(dedent('''
            Section: feature_group_strategies -
            The following given feature group strategies:
            '{}' are invalid. Available strategies are: '{}'
            '''.format(bad_strategies, available_strategies)))


class UserMetadataValidator(Validator):
    def _run(self, user_metadata):
        if not isinstance(user_metadata, dict):
            raise ValueError(dedent('''
            Section: user_metadata -
            user_metadata section must be a dict'''))


class ModelGroupKeysValidator(Validator):
    def _run(self, model_group_keys, user_metadata):
        if not isinstance(model_group_keys, list):
            raise ValueError(dedent('''
            Section: model_group_keys -
            model_group_keys section must be a list'''))
        classifier_keys = [
            'class_path',
            'parameters'
        ]
        # planner_keys are defined in architect.Planner._make_metadata
        planner_keys = [
            'feature_start_time',
            'end_time',
            'indices',
            'feature_names',
            'feature_groups',
            'label_name',
            'label_type',
            'label_timespan',
            'state',
            'cohort_name',
            'matrix_id',
            'matrix_type'
        ]
        # temporal_keys are defined in
        # timechop.Timechop.generate_matrix_definition
        temporal_keys = [
            'first_as_of_time',
            'last_as_of_time',
            'matrix_info_end_time',
            'as_of_times',
            'training_label_timespan',
            'training_as_of_date_frequency',
            'max_training_history'
        ]
        available_keys = [key for key in user_metadata.keys()] + \
            planner_keys +\
            temporal_keys +\
            classifier_keys
        for model_group_key in model_group_keys:
            if model_group_key not in available_keys:
                raise ValueError(dedent('''
                Section: model_group_keys -
                unknown entry '{}' received. Available keys are {}
                '''.format(model_group_key, available_keys)))


class GridConfigValidator(Validator):
    def _run(self, grid_config):
        if not grid_config:
            raise ValueError(dedent('''
            Section: grid_config -
            Section not found. You must define a grid_config.'''))
        for classpath, parameter_config in grid_config.items():
            try:
                module_name, class_name = classpath.rsplit(".", 1)
                module = importlib.import_module(module_name)
                cls = getattr(module, class_name)
                for parameters in ParameterGrid(parameter_config):
                    try:
                        cls(**parameters)
                    except Exception as e:
                        raise ValueError(dedent('''
                        Section: grid_config -
                        Unable to instantiate classifier {} with parameters {}, error thrown: {}
                        '''.format(classpath, parameters, e)))
            except Exception as e:
                raise ValueError(dedent('''
                Section: grid_config -
                Unable to import classifier {}, error thrown: {}
                '''.format(classpath, e)))


class ScoringConfigValidator(Validator):
    def _run(self, scoring_config):
        if 'testing_metric_groups' not in scoring_config:
            logging.warning('Section: scoring - No testing_metric_groups configured. ' +
                            'Your experiment may run, but you will not have any ' +
                            'evaluation metrics computed'
                            )
        if 'training_metric_groups' not in scoring_config:
            logging.warning('Section: scoring - No training_metric_groups configured. ' +
                            'If training set evaluation metrics are desired, they must be added'
                            )
        metric_lookup = catwalk.evaluation.ModelEvaluator.available_metrics
        available_metrics = set(metric_lookup.keys())
        for group in ('testing_metric_groups', 'training_metric_groups'):
            for metric_group in scoring_config.get(group, {}):
                given_metrics = set(metric_group['metrics'])
                bad_metrics = given_metrics - available_metrics
                if bad_metrics:
                    raise ValueError(dedent(
                        '''Section: scoring -
                        The following given metrics '{}' are unavailable. Available metrics are: '{}'
                        '''.format(bad_metrics, available_metrics)))
                for given_metric in given_metrics:
                    metric_function = metric_lookup[given_metric]
                    if not hasattr(metric_function, 'greater_is_better'):
                        raise ValueError(dedent(
                        '''Section: scoring -
                        The metric {} does not define the attribute
                        'greater_is_better'. This can only be fixed in the catwalk.metrics
                        module. If you still would like to use this metric, consider
                        submitting a pull request'''.format(given_metric)))


class ExperimentValidator(Validator):
    def run(self, experiment_config):
        TemporalValidator(strict=self.strict).run(experiment_config.get('temporal_config', {}))
        FeatureAggregationsValidator(self.db_engine, strict=self.strict)\
            .run(experiment_config.get('feature_aggregations', {}))
        LabelConfigValidator(self.db_engine, strict=self.strict).run(
            experiment_config.get('label_config', None))
        CohortConfigValidator(self.db_engine, strict=self.strict).run(
            experiment_config.get('cohort_config', {}))
        FeatureGroupDefinitionValidator(strict=self.strict).run(
            experiment_config.get('feature_group_definition', {}),
            experiment_config.get('feature_aggregations', {})
        )
        FeatureGroupStrategyValidator(strict=self.strict).run(
            experiment_config.get('feature_group_strategies', []),
        )
        UserMetadataValidator(strict=self.strict).run(
            experiment_config.get('user_metadata', {})
        )
        ModelGroupKeysValidator(strict=self.strict).run(
            experiment_config.get('model_group_keys', []),
            experiment_config.get('user_metadata', {})
        )
        GridConfigValidator(strict=self.strict).run(experiment_config.get('grid_config', {}))
        ScoringConfigValidator(strict=self.strict).run(experiment_config.get('scoring', {}))

        # show the success message in the console as well as the logger
        # as we don't really know how they have configured logging
        if self.strict:
            success_message = 'Experiment validation ran to completion with no errors'
        else:
            success_message = 'Experiment validation complete. All configuration problems have been displayed as warnings'
        logging.info(success_message)
        print(success_message)
