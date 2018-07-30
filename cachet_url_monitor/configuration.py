#!/usr/bin/env python
import abc
import copy
import logging
import os
import re
import time

import requests
from yaml import dump
from yaml import load
import json

import latency_unit
import status as st

# This is the mandatory fields that must be in the configuration file in this
# same exact structure.
configuration_mandatory_fields = {
    # 'endpoint': ['url', 'method', 'timeout', 'expectation'],
    'endpoint': ['method', 'timeout', 'expectation'],
    # 'cachet': ['api_url', 'token', 'component_id'],
    'cachet': ['api_url', 'token'],
    'frequency': [],
    'update_urls_frequency': []}

INTUIT_PREPROD_WALKER_URL = "https://config.api.intuit.net/v2/pcgopsweb_walker_preprod-"
INTUIT_PROD_WALKER_URL = "https://config.api.intuit.net/v2/pcgopsweb_walker_prod-"
# INTUIT_URLS = [INTUIT_PREPROD_WALKER_URL, INTUIT_PROD_WALKER_URL]
INTUIT_URLS = [INTUIT_PREPROD_WALKER_URL]
CENTERS = ["qdc", "lvdc"]
# CENTERS = ["lvdc"]

class ConfigurationValidationError(Exception):
    """Exception raised when there's a validation error."""

    def __init__(self, value):
        self.value = value

    def __str__(self):
        return repr(self.value)


class ComponentNonexistentError(Exception):
    """Exception raised when the component does not exist."""

    def __init__(self, component_id):
        self.component_id = component_id

    def __str__(self):
        return repr('Component with id [%d] does not exist.' % (self.component_id,))


class MetricNonexistentError(Exception):
    """Exception raised when the component does not exist."""

    def __init__(self, metric_id):
        self.metric_id = metric_id

    def __str__(self):
        return repr('Metric with id [%d] does not exist.' % (self.metric_id,))


def get_current_status(endpoint_url, component_id, headers):
    """Retrieves the current status of the component that is being monitored. It will fail if the component does
    not exist or doesn't respond with the expected data.
    :return component status.
    """
    get_status_request = requests.get('%s/components/%s' % (endpoint_url, component_id), headers=headers)

    if get_status_request.ok:
        # The component exists.
        return get_status_request.json()['data']['status']
    else:
        raise ComponentNonexistentError(component_id)


def normalize_url(url):
    """If passed url doesn't include schema return it with default one - http."""
    if not url.lower().startswith('http'):
        return 'http://%s' % url
    return url


class Configuration(object):
    """Represents a configuration file, but it also includes the functionality
    of assessing the API and pushing the results to cachet.
    """

    def __init__(self, config_file):
        self.logger = logging.getLogger('cachet_url_monitor.configuration.Configuration')
        self.config_file = config_file
        self.data = load(file(self.config_file, 'r'))
        # self.current_fails = 0
        # self.trigger_update = True

        # Exposing the configuration to confirm it's parsed as expected.
        self.print_out()

        # We need to validate the configuration is correct and then validate the component actually exists.
        self.validate()

        # We store the main information from the configuration file, so we don't keep reading from the data dictionary.
        self.headers = {'X-Cachet-Token': os.environ.get('CACHET_TOKEN') or self.data['cachet']['token']}

        self.endpoint_method = os.environ.get('ENDPOINT_METHOD') or self.data['endpoint']['method']
        # self.endpoint_url = os.environ.get('ENDPOINT_URL') or self.data['endpoint']['url']
        # self.endpoint_url = normalize_url(self.endpoint_url)
        self.endpoint_urls = []
        # self.endpoint_urls.append("https://walker-atesvc2014-tdp-qdc.dckr.intuit.net/atesvc2014/v1/health")
        # self.endpoint_urls.append("https://walker-atesvc2015-e2e-qdc.dckr.intuit.net/atesvc2015/v1/health")
        # self.endpoint_urls.append("https://www.google.com/")
        # for i, url in enumerate(self.endpoint_urls):
            # self.endpoint_urls[i] = normalize_url(url)


        # added version url
        # self.endpoint_version_url = os.environ.get('ENDPOINT_VERSION_URL') or self.data['endpoint']['version_url']
        # if self.endpoint_version_url:
            # self.endpoint_version_url = normalize_url(self.endpoint_version_url)
        # store the build version
        self.versions = []
        self.endpoint_version_urls = []
        # self.endpoint_version_urls.append("https://walker-atesvc2014-e2e-qdc.dckr.intuit.net/atesvc2014/version.txt")
        # self.endpoint_version_urls.append("https://walker-atesvc2015-e2e-qdc.dckr.intuit.net/atesvc2015/version.txt")
        # self.endpoint_version_urls.append("")

        # for i, url in enumerate(self.endpoint_version_urls):
            # if self.endpoint_version_urls[i]:
                # self.endpoint_version_urls[i] = normalize_url(url)
            # self.versions.append("")

        self.endpoint_timeout = os.environ.get('ENDPOINT_TIMEOUT') or self.data['endpoint'].get('timeout') or 1
        self.allowed_fails = os.environ.get('ALLOWED_FAILS') or self.data['endpoint'].get('allowed_fails') or 0

        self.api_url = os.environ.get('CACHET_API_URL') or self.data['cachet']['api_url']
        # self.component_id = os.environ.get('CACHET_COMPONENT_ID') or self.data['cachet']['component_id']
        # self.component_ids = [1, 2, 3]
        self.component_ids = []

        self.get_monitoring_urls()
        self.num_urls = len(self.endpoint_urls)
        # ignore metric for now
        self.metric_id = os.environ.get('CACHET_METRIC_ID') or self.data['cachet'].get('metric_id')

        if self.metric_id is not None:
            self.default_metric_value = self.get_default_metric_value(self.metric_id)

        # The latency_unit configuration is not mandatory and we fallback to seconds, by default.
        self.latency_unit = os.environ.get('LATENCY_UNIT') or self.data['cachet'].get('latency_unit') or 's'

        # We need the current status so we monitor the status changes. This is necessary for creating incidents.
        # self.status = get_current_status(self.api_url, self.component_id, self.headers)

        # Get remaining settings
        self.public_incidents = int(
            os.environ.get('CACHET_PUBLIC_INCIDENTS') or self.data['cachet']['public_incidents'])

        for endpoint_url in self.endpoint_urls:
            self.logger.info('Monitoring URL: %s %s' % (self.endpoint_method, endpoint_url))
        self.expectations = [Expectaction.create(expectation) for expectation in self.data['endpoint']['expectation']]
        for expectation in self.expectations:
            self.logger.info('Registered expectation: %s' % (expectation,))
        
        # initialize other variables
        self.current_timestamps = [-1 for i in range(self.num_urls)]
        self.messages = ["" for i in range(self.num_urls)]
        self.statuses = [get_current_status(self.api_url, self.component_ids[i], self.headers) for i in range(self.num_urls)]
        self.requests = [-1 for i in range(self.num_urls)]
        self.trigger_updates = [False for i in range(self.num_urls)] 
        self.current_fails = [0 for i in range(self.num_urls)]
        self.incident_ids = [-1 for i in range(self.num_urls)]
        self.versions = ["" for i in range(self.num_urls)]

    def get_default_metric_value(self, metric_id):
        """Returns default value for configured metric."""
        get_metric_request = requests.get('%s/metrics/%s' % (self.api_url, metric_id), headers=self.headers)

        if get_metric_request.ok:
            return get_metric_request.json()['data']['default_value']
        else:
            raise MetricNonexistentError(metric_id)
    
    def get_monitoring_urls(self):
        """Obtains the Cachet components and match them to the corresponding urls.
        We only check the urls and update the components found.
        """
        # build Intuit URLs database
        self.logger.info('Updating components and monitoring urls...')
        self.intuit_db = {}
        headers = {'authorization': 'Intuit_IAM_Authentication intuit_appid=Intuit.platform.pcgops-web.pcgopsweb, intuit_app_secret=prdnOkZSA08TRkxqA8uADHs50jtMvqwaEiH3RXSI'}
        for center in CENTERS:
            for url in INTUIT_URLS: 
                url_center = url + center + ".yaml"
                response = requests.request("GET", url_center, headers=headers)
                data = load(response.text)
                if not center in self.intuit_db:
                    self.intuit_db[center] = {} 
                # print data
                for each_entry in data:
                    name = each_entry['name']
                    check_url = each_entry['url']
                    env = each_entry['env']
                    if not 'overlay' in name and not 'walker' in name: 
                        # special case when env is prd because we may have multiple prd environments       
                        if env == 'prd':
                            name, env_num = name.split('_')
                            env = env + env_num[-1]
                        if not name in self.intuit_db[center]:
                            self.intuit_db[center][name] = {}
                        self.intuit_db[center][name][env] = {}
                        self.intuit_db[center][name][env]['url'] = check_url
                        if 'version_url' in each_entry:
                            self.intuit_db[center][name][env]['version_url'] = each_entry['version_url']
                        else:
                            self.intuit_db[center][name][env]['version_url'] = ''
        
        # build Cachet Component database
        url = self.api_url + '/components'
        response = requests.request("GET", url)
        data = json.loads(response.text)
        self.cachet_db= {}
        for center in CENTERS:
            self.cachet_db[center] = {}
        for each_entry in data['data']:
            name = each_entry['name']
            svc_name, data_center, env = name.split('-')
            if not svc_name in self.cachet_db:
                self.cachet_db[data_center][svc_name] = {}
            self.cachet_db[data_center][svc_name][env] = each_entry['id']
        
        # print self.intuit_db
        # print self.cachet_db

        # match the URLs
        self.component_ids = []
        self.endpoint_urls = []
        self.endpoint_version_urls = []
        for center in CENTERS:
            for svc in self.cachet_db[center]:
                for env in self.cachet_db[center][svc]:
                    if svc in self.intuit_db[center]:
                        if env in self.intuit_db[center][svc]:
                            self.component_ids.append(self.cachet_db[center][svc][env])
                            self.endpoint_urls.append(self.intuit_db[center][svc][env]['url'])
                            self.endpoint_version_urls.append(self.intuit_db[center][svc][env]['version_url'])
        
        self.num_urls = len(self.component_ids)
        # print "Number of urls:", self.num_urls
        # for i in range(self.num_urls):
            # print self.component_ids[i], self.endpoint_urls[i], self.endpoint_version_urls[i]
        

    def get_action(self):
        """Retrieves the action list from the configuration. If it's empty, returns an empty list.
        :return: The list of actions, which can be an empty list.
        """
        if self.data['cachet'].get('action') is None:
            return []
        else:
            return self.data['cachet']['action']

    def validate(self):
        """Validates the configuration by verifying the mandatory fields are
        present and in the correct format. If the validation fails, a
        ConfigurationValidationError is raised. Otherwise nothing will happen.
        """
        configuration_errors = []
        for key, sub_entries in configuration_mandatory_fields.iteritems():
            if key not in self.data:
                configuration_errors.append(key)

            for sub_key in sub_entries:
                if sub_key not in self.data[key]:
                    configuration_errors.append('%s.%s' % (key, sub_key))

        # we don't validate endpoint because we will provide the endpoint urls here, not in the config
        # if ('endpoint' in self.data and 'expectation' in
        #     self.data['endpoint']):
        #     if (not isinstance(self.data['endpoint']['expectation'], list) or
        #             (isinstance(self.data['endpoint']['expectation'], list) and
        #                      len(self.data['endpoint']['expectation']) == 0)):
        #         configuration_errors.append('endpoint.expectation')

        if len(configuration_errors) > 0:
            raise ConfigurationValidationError(
                'Config file [%s] failed validation. Missing keys: %s' % (self.config_file,
                                                                          ', '.join(configuration_errors)))

    def evaluate(self):
        """Sends the request to the URL set in the configuration and executes
        each one of the expectations, one by one. The status will be updated
        according to the expectation results.
        """
        for i in range(self.num_urls):
            try:
                self.requests[i] = requests.request(self.endpoint_method, self.endpoint_urls[i], timeout=self.endpoint_timeout)
                self.current_timestamps[i] = int(time.time())
            except requests.ConnectionError:
                self.messages[i] = 'The URL is unreachable: %s %s' % (self.endpoint_method, self.endpoint_urls[i])
                self.logger.warning(self.messages[i])
                self.statuses[i] = st.COMPONENT_STATUS_PARTIAL_OUTAGE
                return
            except requests.HTTPError:
                self.messages[i] = 'Unexpected HTTP response'
                self.logger.exception(self.messages[i])
                self.statuses[i] = st.COMPONENT_STATUS_PARTIAL_OUTAGE
                return
            except requests.Timeout:
                self.messages[i] = 'Request timed out'
                self.logger.warning(self.messages[i])
                self.statuses[i] = st.COMPONENT_STATUS_PERFORMANCE_ISSUES
                return

            # obtain the build version
            if (self.endpoint_version_urls[i]):
                r = requests.get(self.endpoint_version_urls[i])
                if r.status_code == requests.codes.ok:
                    self.versions[i] = r.text.split('-->')[0]
                else:
                    self.versions[i] = 'Unknown'

            # We initially assume the API is healthy.
            self.statuses[i] = st.COMPONENT_STATUS_OPERATIONAL
            self.messages[i] = ''
            for expectation in self.expectations:
                status = expectation.get_status(self.requests[i])
                # self.logger.info('Component %d check: expectation status [%d]' % (self.component_ids[i], status,))
                # The greater the status is, the worse the state of the API is.
                if status > self.statuses[i]:
                    self.statuses[i] = status
                    self.messages[i] = expectation.get_message(self.requests[i])
                    self.logger.info(self.messages[i])

    def print_out(self):
        self.logger.info('Current configuration:\n%s' % (self.__repr__()))

    def __repr__(self):
        temporary_data = copy.deepcopy(self.data)
        # Removing the token so we don't leak it in the logs.
        del temporary_data['cachet']['token']
        return dump(temporary_data, default_flow_style=False)

    def if_trigger_update(self):
        """
        Checks if update should be triggered - trigger it for all operational states
        and only for non-operational ones above the configured threshold (allowed_fails).
        """
        for i in range(self.num_urls):
            if self.statuses[i] != 1:
                self.current_fails[i] = self.current_fails[i] + 1
                self.logger.info('Failure #%s with threshold set to %s' % (self.current_fails[i], self.allowed_fails))
                if self.current_fails[i] <= self.allowed_fails:
                    self.trigger_updates[i] = False
                    return
            self.current_fails[i] = 0
            self.trigger_updates[i] = True

    def push_status(self):
        """Pushes the status of the component to the cachet server. It will update the component
        status based on the previous call to evaluate().
        """
        for i in range(self.num_urls):
            if not self.trigger_updates[i]:
                return
            # added push version number to the description        
            params = {'id': self.component_ids[i], 'status': self.statuses[i], 'description': self.versions[i]}
            component_request = requests.put('%s/components/%d' % (self.api_url, self.component_ids[i]), params=params,
                                            headers=self.headers)
            if component_request.ok:
                # Successful update
                self.logger.info('Component %d update: status [%d]' % (self.component_ids[i], self.statuses[i],))
            else:
                # Failed to update the API status
                self.logger.warning('Component %d update failed with status [%d]: API'
                                    ' status: [%d]' % (self.component_ids[i], component_request.status_code, self.statuses[i]))

    def push_metrics(self):
        """Pushes the total amount of seconds the request took to get a response from the URL.
        It only will send a request if the metric id was set in the configuration.
        In case of failed connection trial pushes the default metric value.
        """
        if 'metric_id' in self.data['cachet'] and hasattr(self, 'request'):
            # We convert the elapsed time from the request, in seconds, to the configured unit.
            value = self.default_metric_value if self.status != 1 else latency_unit.convert_to_unit(self.latency_unit,
                                                                                                    self.request.elapsed.total_seconds())
            params = {'id': self.metric_id, 'value': value,
                      'timestamp': self.current_timestamp}
            metrics_request = requests.post('%s/metrics/%d/points' % (self.api_url, self.metric_id), params=params,
                                            headers=self.headers)

            if metrics_request.ok:
                # Successful metrics upload
                self.logger.info('Metric uploaded: %.6f seconds' % (value,))
            else:
                self.logger.warning('Metric upload failed with status [%d]' %
                                    (metrics_request.status_code,))

    def push_incident(self):
        """If the component status has changed, we create a new incident (if this is the first time it becomes unstable)
        or updates the existing incident once it becomes healthy again.
        """
        for i in range(self.num_urls):
            if not self.trigger_updates[i]:
                return
            # if hasattr(self, 'incident_id') and self.statuses[i] == st.COMPONENT_STATUS_OPERATIONAL:
            if self.incident_ids[i] != -1 and self.statuses[i] == st.COMPONENT_STATUS_OPERATIONAL:
                # If the incident already exists, it means it was unhealthy but now it's healthy again.
                params = {'status': 4, 'visible': self.public_incidents, 'component_id': self.component_ids[i],
                        'component_status': self.statuses[i],
                        'notify': True}

                incident_request = requests.put('%s/incidents/%d' % (self.api_url, self.incident_ids[i]), params=params,
                                                headers=self.headers)
                if incident_request.ok:
                    # Successful metrics upload
                    self.logger.info(
                        'Incident updated, API healthy again: component status [%d], message: "%s"' % (
                            self.statuses[i], self.messages[i]))
                    # del self.incident_id
                    self.incident_ids[i] = -1
                else:
                    self.logger.warning('Incident update failed with status [%d], message: "%s"' % (
                        incident_request.status_code, self.messages[i]))
            # elif not hasattr(self, 'incident_id') and self.statuses[i] != st.COMPONENT_STATUS_OPERATIONAL:
            elif self.incident_ids[i] != -1 and self.statuses[i] != st.COMPONENT_STATUS_OPERATIONAL:
                # This is the first time the incident is being created.
                params = {'name': 'URL unavailable', 'message': self.messages[i], 'status': 1, 'visible': self.public_incidents,
                        'component_id': self.component_ids[i], 'component_status': self.statuses[i], 'notify': True}
                incident_request = requests.post('%s/incidents' % (self.api_url,), params=params, headers=self.headers)
                if incident_request.ok:
                    # Successful incident upload.
                    self.incident_ids[i] = incident_request.json()['data']['id']
                    self.logger.info(
                        'Incident uploaded, API unhealthy: component status [%d], message: "%s"' % (
                            self.statuses[i], self.messages[i]))
                else:
                    self.logger.warning(
                        'Incident upload failed with status [%d], message: "%s"' % (
                            incident_request.status_code, self.messages[i]))


class Expectaction(object):
    """Base class for URL result expectations. Any new excpectation should extend
    this class and the name added to create() method.
    """

    @staticmethod
    def create(configuration):
        """Creates a list of expectations based on the configuration types
        list.
        """
        expectations = {
            'HTTP_STATUS': HttpStatus,
            'LATENCY': Latency,
            'REGEX': Regex
        }
        return expectations.get(configuration['type'])(configuration)

    @abc.abstractmethod
    def get_status(self, response):
        """Returns the status of the API, following cachet's component status
        documentation: https://docs.cachethq.io/docs/component-statuses
        """

    @abc.abstractmethod
    def get_message(self, response):
        """Gets the error message."""


class HttpStatus(Expectaction):
    def __init__(self, configuration):
        self.status_range = HttpStatus.parse_range(configuration['status_range'])

    @staticmethod
    def parse_range(range_string):
        statuses = range_string.split("-")
        if len(statuses) == 1:
            # When there was no range given, we should treat the first number as a single status check.
            return (int(statuses[0]), int(statuses[0]) + 1)
        else:
            # We shouldn't look into more than one value, as this is a range value.
            return (int(statuses[0]), int(statuses[1]))

    def get_status(self, response):
        if response.status_code >= self.status_range[0] and response.status_code < self.status_range[1]:
            return st.COMPONENT_STATUS_OPERATIONAL
        else:
            return st.COMPONENT_STATUS_PARTIAL_OUTAGE

    def get_message(self, response):
        return 'Unexpected HTTP status (%s)' % (response.status_code,)

    def __str__(self):
        return repr('HTTP status range: %s' % (self.status_range,))


class Latency(Expectaction):
    def __init__(self, configuration):
        self.threshold = configuration['threshold']

    def get_status(self, response):
        if response.elapsed.total_seconds() <= self.threshold:
            return st.COMPONENT_STATUS_OPERATIONAL
        else:
            return st.COMPONENT_STATUS_PERFORMANCE_ISSUES

    def get_message(self, response):
        return 'Latency above threshold: %.4f seconds' % (response.elapsed.total_seconds(),)

    def __str__(self):
        return repr('Latency threshold: %.4f seconds' % (self.threshold,))


class Regex(Expectaction):
    def __init__(self, configuration):
        self.regex_string = configuration['regex']
        self.regex = re.compile(configuration['regex'], re.UNICODE + re.DOTALL)

    def get_status(self, response):
        if self.regex.match(response.text):
            return st.COMPONENT_STATUS_OPERATIONAL
        else:
            return st.COMPONENT_STATUS_PARTIAL_OUTAGE

    def get_message(self, response):
        return 'Regex did not match anything in the body'

    def __str__(self):
        return repr('Regex: %s' % (self.regex_string,))
