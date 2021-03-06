# Copyright 2014 OpenStack Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import random

import mock
from neutron_lib import constants
from oslo_config import cfg
from oslo_utils import importutils
import testscenarios

from neutron import context
from neutron.db import agentschedulers_db as sched_db
from neutron.db import common_db_mixin
from neutron.db import models_v2
from neutron.db.network_dhcp_agent_binding import models as ndab_model
from neutron.extensions import dhcpagentscheduler
from neutron.scheduler import dhcp_agent_scheduler
from neutron.services.segments import db as segments_service_db
from neutron.tests.common import helpers
from neutron.tests.unit.plugins.ml2 import test_plugin
from neutron.tests.unit import testlib_api

# Required to generate tests from scenarios. Not compatible with nose.
load_tests = testscenarios.load_tests_apply_scenarios

HOST_C = 'host-c'
HOST_D = 'host-d'


class TestDhcpSchedulerBaseTestCase(testlib_api.SqlTestCase):

    def setUp(self):
        super(TestDhcpSchedulerBaseTestCase, self).setUp()
        self.ctx = context.get_admin_context()
        self.network = {'id': 'foo_network_id'}
        self.network_id = 'foo_network_id'
        self._save_networks([self.network_id])

    def _create_and_set_agents_down(self, hosts, down_agent_count=0,
                                    admin_state_up=True,
                                    az=helpers.DEFAULT_AZ):
        agents = []
        for i, host in enumerate(hosts):
            is_alive = i >= down_agent_count
            agents.append(helpers.register_dhcp_agent(
                host,
                admin_state_up=admin_state_up,
                alive=is_alive,
                az=az))
        return agents

    def _save_networks(self, networks):
        for network_id in networks:
            with self.ctx.session.begin(subtransactions=True):
                self.ctx.session.add(models_v2.Network(id=network_id))

    def _test_schedule_bind_network(self, agents, network_id):
        scheduler = dhcp_agent_scheduler.ChanceScheduler()
        scheduler.resource_filter.bind(self.ctx, agents, network_id)
        results = self.ctx.session.query(
            ndab_model.NetworkDhcpAgentBinding).filter_by(
            network_id=network_id).all()
        self.assertEqual(len(agents), len(results))
        for result in results:
            self.assertEqual(network_id, result.network_id)


class TestDhcpScheduler(TestDhcpSchedulerBaseTestCase):

    def test_schedule_bind_network_single_agent(self):
        agents = self._create_and_set_agents_down(['host-a'])
        self._test_schedule_bind_network(agents, self.network_id)

    def test_schedule_bind_network_multi_agents(self):
        agents = self._create_and_set_agents_down(['host-a', 'host-b'])
        self._test_schedule_bind_network(agents, self.network_id)

    def test_schedule_bind_network_multi_agent_fail_one(self):
        agents = self._create_and_set_agents_down(['host-a'])
        self._test_schedule_bind_network(agents, self.network_id)
        with mock.patch.object(dhcp_agent_scheduler.LOG, 'info') as fake_log:
            self._test_schedule_bind_network(agents, self.network_id)
            self.assertEqual(1, fake_log.call_count)

    def _test_get_agents_and_scheduler_for_dead_agent(self):
        agents = self._create_and_set_agents_down(['dead_host', 'alive_host'],
                                                  1)
        dead_agent = [agents[0]]
        alive_agent = [agents[1]]
        self._test_schedule_bind_network(dead_agent, self.network_id)
        scheduler = dhcp_agent_scheduler.ChanceScheduler()
        return dead_agent, alive_agent, scheduler

    def _test_reschedule_vs_network_on_dead_agent(self,
                                                  active_hosts_only):
        dead_agent, alive_agent, scheduler = (
            self._test_get_agents_and_scheduler_for_dead_agent())
        network = {'id': self.network_id}
        plugin = mock.Mock()
        plugin.get_subnets.return_value = [{"network_id": self.network_id,
                                            "enable_dhcp": True}]
        plugin.get_agents_db.return_value = dead_agent + alive_agent
        plugin.filter_hosts_with_network_access.side_effect = (
            lambda context, network_id, hosts: hosts)
        if active_hosts_only:
            plugin.get_dhcp_agents_hosting_networks.return_value = []
            self.assertTrue(
                scheduler.schedule(
                    plugin, self.ctx, network))
        else:
            plugin.get_dhcp_agents_hosting_networks.return_value = dead_agent
            self.assertFalse(
                scheduler.schedule(
                    plugin, self.ctx, network))

    def test_network_rescheduled_when_db_returns_active_hosts(self):
        self._test_reschedule_vs_network_on_dead_agent(True)

    def test_network_not_rescheduled_when_db_returns_all_hosts(self):
        self._test_reschedule_vs_network_on_dead_agent(False)

    def _get_agent_binding_from_db(self, agent):
        return self.ctx.session.query(
            ndab_model.NetworkDhcpAgentBinding
        ).filter_by(dhcp_agent_id=agent[0].id).all()

    def _test_auto_reschedule_vs_network_on_dead_agent(self,
                                                       active_hosts_only):
        dead_agent, alive_agent, scheduler = (
            self._test_get_agents_and_scheduler_for_dead_agent())
        plugin = mock.Mock()
        plugin.get_subnets.return_value = [{"network_id": self.network_id,
                                            "enable_dhcp": True}]
        plugin.get_network.return_value = self.network
        if active_hosts_only:
            plugin.get_dhcp_agents_hosting_networks.return_value = []
        else:
            plugin.get_dhcp_agents_hosting_networks.return_value = dead_agent
        network_assigned_to_dead_agent = (
            self._get_agent_binding_from_db(dead_agent))
        self.assertEqual(1, len(network_assigned_to_dead_agent))
        self.assertTrue(
            scheduler.auto_schedule_networks(
                plugin, self.ctx, "alive_host"))
        network_assigned_to_dead_agent = (
            self._get_agent_binding_from_db(dead_agent))
        network_assigned_to_alive_agent = (
            self._get_agent_binding_from_db(alive_agent))
        self.assertEqual(1, len(network_assigned_to_dead_agent))
        if active_hosts_only:
            self.assertEqual(1, len(network_assigned_to_alive_agent))
        else:
            self.assertEqual(0, len(network_assigned_to_alive_agent))

    def test_network_auto_rescheduled_when_db_returns_active_hosts(self):
        self._test_auto_reschedule_vs_network_on_dead_agent(True)

    def test_network_not_auto_rescheduled_when_db_returns_all_hosts(self):
        self._test_auto_reschedule_vs_network_on_dead_agent(False)


class TestAutoScheduleNetworks(TestDhcpSchedulerBaseTestCase):
    """Unit test scenarios for ChanceScheduler.auto_schedule_networks.

    network_present
        Network is present or not

    enable_dhcp
        Dhcp is enabled or disabled in the subnet of the network

    scheduled_already
        Network is already scheduled to the agent or not

    agent_down
        Dhcp agent is down or alive

    valid_host
        If true, then an valid host is passed to schedule the network,
        else an invalid host is passed.

    az_hints
        'availability_zone_hints' of the network.
        note that default 'availability_zone' of an agent is 'nova'.
    """
    scenarios = [
        ('Network present',
         dict(network_present=True,
              enable_dhcp=True,
              scheduled_already=False,
              agent_down=False,
              valid_host=True,
              az_hints=[])),

        ('No network',
         dict(network_present=False,
              enable_dhcp=False,
              scheduled_already=False,
              agent_down=False,
              valid_host=True,
              az_hints=[])),

        ('Network already scheduled',
         dict(network_present=True,
              enable_dhcp=True,
              scheduled_already=True,
              agent_down=False,
              valid_host=True,
              az_hints=[])),

        ('Agent down',
         dict(network_present=True,
              enable_dhcp=True,
              scheduled_already=False,
              agent_down=False,
              valid_host=True,
              az_hints=[])),

        ('dhcp disabled',
         dict(network_present=True,
              enable_dhcp=False,
              scheduled_already=False,
              agent_down=False,
              valid_host=False,
              az_hints=[])),

        ('Invalid host',
         dict(network_present=True,
              enable_dhcp=True,
              scheduled_already=False,
              agent_down=False,
              valid_host=False,
              az_hints=[])),

        ('Match AZ',
         dict(network_present=True,
              enable_dhcp=True,
              scheduled_already=False,
              agent_down=False,
              valid_host=True,
              az_hints=['nova'])),

        ('Not match AZ',
         dict(network_present=True,
              enable_dhcp=True,
              scheduled_already=False,
              agent_down=False,
              valid_host=True,
              az_hints=['not-match'])),
    ]

    def test_auto_schedule_network(self):
        plugin = mock.MagicMock()
        plugin.get_subnets.return_value = (
            [{"network_id": self.network_id, "enable_dhcp": self.enable_dhcp}]
            if self.network_present else [])
        plugin.get_network.return_value = {'availability_zone_hints':
                                           self.az_hints}
        scheduler = dhcp_agent_scheduler.ChanceScheduler()
        if self.network_present:
            down_agent_count = 1 if self.agent_down else 0
            agents = self._create_and_set_agents_down(
                ['host-a'], down_agent_count=down_agent_count)
            if self.scheduled_already:
                self._test_schedule_bind_network(agents, self.network_id)

        expected_result = (self.network_present and self.enable_dhcp)
        expected_hosted_agents = (1 if expected_result and
                                  self.valid_host else 0)
        if (self.az_hints and
            agents[0]['availability_zone'] not in self.az_hints):
            expected_hosted_agents = 0
        host = "host-a" if self.valid_host else "host-b"
        observed_ret_value = scheduler.auto_schedule_networks(
            plugin, self.ctx, host)
        self.assertEqual(expected_result, observed_ret_value)
        hosted_agents = self.ctx.session.query(
            ndab_model.NetworkDhcpAgentBinding).all()
        self.assertEqual(expected_hosted_agents, len(hosted_agents))


class TestNetworksFailover(TestDhcpSchedulerBaseTestCase,
                           sched_db.DhcpAgentSchedulerDbMixin,
                           common_db_mixin.CommonDbMixin):
    def test_reschedule_network_from_down_agent(self):
        agents = self._create_and_set_agents_down(['host-a', 'host-b'], 1)
        self._test_schedule_bind_network([agents[0]], self.network_id)
        self._save_networks(["foo-network-2"])
        self._test_schedule_bind_network([agents[1]], "foo-network-2")
        with mock.patch.object(self, 'remove_network_from_dhcp_agent') as rn,\
                mock.patch.object(self,
                                  'schedule_network',
                                  return_value=[agents[1]]) as sch,\
                mock.patch.object(self,
                                  'get_network',
                                  create=True,
                                  return_value={'id': self.network_id}):
            notifier = mock.MagicMock()
            self.agent_notifiers[constants.AGENT_TYPE_DHCP] = notifier
            self.remove_networks_from_down_agents()
            rn.assert_called_with(mock.ANY, agents[0].id, self.network_id,
                                  notify=False)
            sch.assert_called_with(mock.ANY, {'id': self.network_id})
            notifier.network_added_to_agent.assert_called_with(
                mock.ANY, self.network_id, agents[1].host)

    def _test_failed_rescheduling(self, rn_side_effect=None):
        agents = self._create_and_set_agents_down(['host-a', 'host-b'], 1)
        self._test_schedule_bind_network([agents[0]], self.network_id)
        with mock.patch.object(self,
                               'remove_network_from_dhcp_agent',
                               side_effect=rn_side_effect) as rn,\
                mock.patch.object(self,
                                  'schedule_network',
                                  return_value=None) as sch,\
                mock.patch.object(self,
                                  'get_network',
                                  create=True,
                                  return_value={'id': self.network_id}):
            notifier = mock.MagicMock()
            self.agent_notifiers[constants.AGENT_TYPE_DHCP] = notifier
            self.remove_networks_from_down_agents()
            rn.assert_called_with(mock.ANY, agents[0].id, self.network_id,
                                  notify=False)
            sch.assert_called_with(mock.ANY, {'id': self.network_id})
            self.assertFalse(notifier.network_added_to_agent.called)

    def test_reschedule_network_from_down_agent_failed(self):
        self._test_failed_rescheduling()

    def test_reschedule_network_from_down_agent_concurrent_removal(self):
        self._test_failed_rescheduling(
            rn_side_effect=dhcpagentscheduler.NetworkNotHostedByDhcpAgent(
                network_id='foo', agent_id='bar'))

    def test_filter_bindings(self):
        bindings = [
            ndab_model.NetworkDhcpAgentBinding(network_id='foo1',
                                               dhcp_agent={'id': 'id1'}),
            ndab_model.NetworkDhcpAgentBinding(network_id='foo2',
                                               dhcp_agent={'id': 'id1'}),
            ndab_model.NetworkDhcpAgentBinding(network_id='foo3',
                                               dhcp_agent={'id': 'id2'}),
            ndab_model.NetworkDhcpAgentBinding(network_id='foo4',
                                               dhcp_agent={'id': 'id2'})]
        with mock.patch.object(self, 'agent_starting_up',
                               side_effect=[True, False]):
            res = [b for b in self._filter_bindings(None, bindings)]
            # once per each agent id1 and id2
            self.assertEqual(2, len(res))
            res_ids = [b.network_id for b in res]
            self.assertIn('foo3', res_ids)
            self.assertIn('foo4', res_ids)

    def test_reschedule_network_from_down_agent_failed_on_unexpected(self):
        agents = self._create_and_set_agents_down(['host-a'], 1)
        self._test_schedule_bind_network([agents[0]], self.network_id)
        with mock.patch.object(
            self, '_filter_bindings',
            side_effect=Exception()):
            # just make sure that no exception is raised
            self.remove_networks_from_down_agents()

    def test_reschedule_network_catches_exceptions_on_fetching_bindings(self):
        with mock.patch('neutron.context.get_admin_context') as get_ctx:
            mock_ctx = mock.Mock()
            get_ctx.return_value = mock_ctx
            mock_ctx.session.query.side_effect = Exception()
            # just make sure that no exception is raised
            self.remove_networks_from_down_agents()

    def test_reschedule_doesnt_occur_if_no_agents(self):
        agents = self._create_and_set_agents_down(['host-a', 'host-b'], 2)
        self._test_schedule_bind_network([agents[0]], self.network_id)
        with mock.patch.object(
            self, 'remove_network_from_dhcp_agent') as rn:
            self.remove_networks_from_down_agents()
            self.assertFalse(rn.called)


class DHCPAgentWeightSchedulerTestCase(test_plugin.Ml2PluginV2TestCase):
    """Unit test scenarios for WeightScheduler.schedule."""

    def setUp(self):
        super(DHCPAgentWeightSchedulerTestCase, self).setUp()
        weight_scheduler = (
            'neutron.scheduler.dhcp_agent_scheduler.WeightScheduler')
        cfg.CONF.set_override('network_scheduler_driver', weight_scheduler)
        self.plugin = importutils.import_object('neutron.plugins.ml2.plugin.'
                                                'Ml2Plugin')
        mock.patch.object(
            self.plugin, 'filter_hosts_with_network_access',
            side_effect=lambda context, network_id, hosts: hosts).start()
        self.plugin.network_scheduler = importutils.import_object(
            weight_scheduler)
        cfg.CONF.set_override("dhcp_load_type", "networks")
        self.segments_plugin = importutils.import_object(
            'neutron.services.segments.plugin.Plugin')
        self.ctx = context.get_admin_context()

    def _create_network(self):
        net = self.plugin.create_network(
            self.ctx,
            {'network': {'name': 'name',
                         'tenant_id': 'tenant_one',
                         'admin_state_up': True,
                         'shared': True}})
        return net['id']

    def _create_segment(self, network_id):
        seg = self.segments_plugin.create_segment(
            self.ctx,
            {'segment': {'network_id': network_id,
                         'physical_network': constants.ATTR_NOT_SPECIFIED,
                         'network_type': 'meh',
                         'segmentation_id': constants.ATTR_NOT_SPECIFIED}})
        return seg['id']

    def test_scheduler_one_agents_per_network(self):
        net_id = self._create_network()
        helpers.register_dhcp_agent(HOST_C)
        self.plugin.network_scheduler.schedule(self.plugin, self.ctx,
                                               {'id': net_id})
        agents = self.plugin.get_dhcp_agents_hosting_networks(self.ctx,
                                                              [net_id])
        self.assertEqual(1, len(agents))

    def test_scheduler_two_agents_per_network(self):
        cfg.CONF.set_override('dhcp_agents_per_network', 2)
        net_id = self._create_network()
        helpers.register_dhcp_agent(HOST_C)
        helpers.register_dhcp_agent(HOST_D)
        self.plugin.network_scheduler.schedule(self.plugin, self.ctx,
                                               {'id': net_id})
        agents = self.plugin.get_dhcp_agents_hosting_networks(self.ctx,
                                                              [net_id])
        self.assertEqual(2, len(agents))

    def test_scheduler_no_active_agents(self):
        net_id = self._create_network()
        self.plugin.network_scheduler.schedule(self.plugin, self.ctx,
                                               {'id': net_id})
        agents = self.plugin.get_dhcp_agents_hosting_networks(self.ctx,
                                                              [net_id])
        self.assertEqual(0, len(agents))

    def test_scheduler_equal_distribution(self):
        net_id_1 = self._create_network()
        net_id_2 = self._create_network()
        net_id_3 = self._create_network()
        helpers.register_dhcp_agent(HOST_C)
        helpers.register_dhcp_agent(HOST_D, networks=1)
        self.plugin.network_scheduler.schedule(
            self.plugin, context.get_admin_context(), {'id': net_id_1})
        helpers.register_dhcp_agent(HOST_D, networks=2)
        self.plugin.network_scheduler.schedule(
            self.plugin, context.get_admin_context(), {'id': net_id_2})
        helpers.register_dhcp_agent(HOST_C, networks=4)
        self.plugin.network_scheduler.schedule(
            self.plugin, context.get_admin_context(), {'id': net_id_3})
        agent1 = self.plugin.get_dhcp_agents_hosting_networks(
            self.ctx, [net_id_1])
        agent2 = self.plugin.get_dhcp_agents_hosting_networks(
            self.ctx, [net_id_2])
        agent3 = self.plugin.get_dhcp_agents_hosting_networks(
            self.ctx, [net_id_3])
        self.assertEqual('host-c', agent1[0]['host'])
        self.assertEqual('host-c', agent2[0]['host'])
        self.assertEqual('host-d', agent3[0]['host'])

    def test_schedule_segment_one_hostable_agent(self):
        net_id = self._create_network()
        seg_id = self._create_segment(net_id)
        helpers.register_dhcp_agent(HOST_C)
        helpers.register_dhcp_agent(HOST_D)
        segments_service_db.update_segment_host_mapping(
            self.ctx, HOST_C, {seg_id})
        net = self.plugin.get_network(self.ctx, net_id)
        seg = self.segments_plugin.get_segment(self.ctx, seg_id)
        net['candidate_hosts'] = seg['hosts']
        agents = self.plugin.network_scheduler.schedule(
            self.plugin, self.ctx, net)
        self.assertEqual(1, len(agents))
        self.assertEqual(HOST_C, agents[0].host)

    def test_schedule_segment_many_hostable_agents(self):
        net_id = self._create_network()
        seg_id = self._create_segment(net_id)
        helpers.register_dhcp_agent(HOST_C)
        helpers.register_dhcp_agent(HOST_D)
        segments_service_db.update_segment_host_mapping(
            self.ctx, HOST_C, {seg_id})
        segments_service_db.update_segment_host_mapping(
            self.ctx, HOST_D, {seg_id})
        net = self.plugin.get_network(self.ctx, net_id)
        seg = self.segments_plugin.get_segment(self.ctx, seg_id)
        net['candidate_hosts'] = seg['hosts']
        agents = self.plugin.network_scheduler.schedule(
            self.plugin, self.ctx, net)
        self.assertEqual(1, len(agents))
        self.assertIn(agents[0].host, [HOST_C, HOST_D])

    def test_schedule_segment_no_host_mapping(self):
        net_id = self._create_network()
        seg_id = self._create_segment(net_id)
        helpers.register_dhcp_agent(HOST_C)
        helpers.register_dhcp_agent(HOST_D)
        net = self.plugin.get_network(self.ctx, net_id)
        seg = self.segments_plugin.get_segment(self.ctx, seg_id)
        net['candidate_hosts'] = seg['hosts']
        agents = self.plugin.network_scheduler.schedule(
            self.plugin, self.ctx, net)
        self.assertEqual(0, len(agents))

    def test_schedule_segment_two_agents_per_segment(self):
        cfg.CONF.set_override('dhcp_agents_per_network', 2)
        net_id = self._create_network()
        seg_id = self._create_segment(net_id)
        helpers.register_dhcp_agent(HOST_C)
        helpers.register_dhcp_agent(HOST_D)
        segments_service_db.update_segment_host_mapping(
            self.ctx, HOST_C, {seg_id})
        segments_service_db.update_segment_host_mapping(
            self.ctx, HOST_D, {seg_id})
        net = self.plugin.get_network(self.ctx, net_id)
        seg = self.segments_plugin.get_segment(self.ctx, seg_id)
        net['candidate_hosts'] = seg['hosts']
        agents = self.plugin.network_scheduler.schedule(
            self.plugin, self.ctx, net)
        self.assertEqual(2, len(agents))
        self.assertIn(agents[0].host, [HOST_C, HOST_D])
        self.assertIn(agents[1].host, [HOST_C, HOST_D])

    def test_schedule_segment_two_agents_per_segment_one_hostable_agent(self):
        cfg.CONF.set_override('dhcp_agents_per_network', 2)
        net_id = self._create_network()
        seg_id = self._create_segment(net_id)
        helpers.register_dhcp_agent(HOST_C)
        helpers.register_dhcp_agent(HOST_D)
        segments_service_db.update_segment_host_mapping(
            self.ctx, HOST_C, {seg_id})
        net = self.plugin.get_network(self.ctx, net_id)
        seg = self.segments_plugin.get_segment(self.ctx, seg_id)
        net['candidate_hosts'] = seg['hosts']
        agents = self.plugin.network_scheduler.schedule(
            self.plugin, self.ctx, net)
        self.assertEqual(1, len(agents))
        self.assertEqual(HOST_C, agents[0].host)


class TestDhcpSchedulerFilter(TestDhcpSchedulerBaseTestCase,
                              sched_db.DhcpAgentSchedulerDbMixin):
    def _test_get_dhcp_agents_hosting_networks(self, expected, **kwargs):
        agents = self._create_and_set_agents_down(['host-a', 'host-b'], 1)
        agents += self._create_and_set_agents_down(['host-c', 'host-d'], 1,
                                                   admin_state_up=False)
        networks = kwargs.pop('networks', [self.network_id])
        for network in networks:
            self._test_schedule_bind_network(agents, network)
        agents = self.get_dhcp_agents_hosting_networks(self.ctx,
                                                       networks,
                                                       **kwargs)
        host_ids = set(a['host'] for a in agents)
        self.assertEqual(expected, host_ids)

    def test_get_dhcp_agents_hosting_networks_default(self):
        self._test_get_dhcp_agents_hosting_networks({'host-a', 'host-b',
                                                     'host-c', 'host-d'})

    def test_get_dhcp_agents_hosting_networks_active(self):
        self._test_get_dhcp_agents_hosting_networks({'host-b', 'host-d'},
                                                    active=True)

    def test_get_dhcp_agents_hosting_networks_admin_up(self):
        self._test_get_dhcp_agents_hosting_networks({'host-a', 'host-b'},
                                                    admin_state_up=True)

    def test_get_dhcp_agents_hosting_networks_active_admin_up(self):
        self._test_get_dhcp_agents_hosting_networks({'host-b'},
                                                    active=True,
                                                    admin_state_up=True)

    def test_get_dhcp_agents_hosting_networks_admin_down(self):
        self._test_get_dhcp_agents_hosting_networks({'host-c', 'host-d'},
                                                    admin_state_up=False)

    def test_get_dhcp_agents_hosting_networks_active_admin_down(self):
        self._test_get_dhcp_agents_hosting_networks({'host-d'},
                                                    active=True,
                                                    admin_state_up=False)

    def test_get_dhcp_agents_hosting_many_networks(self):
        net_id = 'another-net-id'
        self._save_networks([net_id])
        networks = [net_id, self.network_id]
        self._test_get_dhcp_agents_hosting_networks({'host-a', 'host-b',
                                                     'host-c', 'host-d'},
                                                    networks=networks)

    def test_get_dhcp_agents_host_network_filter_by_hosts(self):
        self._test_get_dhcp_agents_hosting_networks({'host-a'},
                                                    hosts=['host-a'])


class DHCPAgentAZAwareWeightSchedulerTestCase(TestDhcpSchedulerBaseTestCase):

    def setUp(self):
        super(DHCPAgentAZAwareWeightSchedulerTestCase, self).setUp()
        self.setup_coreplugin('ml2')
        cfg.CONF.set_override("network_scheduler_driver",
            'neutron.scheduler.dhcp_agent_scheduler.AZAwareWeightScheduler')
        self.plugin = importutils.import_object('neutron.plugins.ml2.plugin.'
                                                'Ml2Plugin')
        mock.patch.object(
            self.plugin, 'filter_hosts_with_network_access',
            side_effect=lambda context, network_id, hosts: hosts).start()
        cfg.CONF.set_override('dhcp_agents_per_network', 1)
        cfg.CONF.set_override("dhcp_load_type", "networks")

    def test_az_scheduler_one_az_hints(self):
        self._save_networks(['1111'])
        helpers.register_dhcp_agent('az1-host1', networks=1, az='az1')
        helpers.register_dhcp_agent('az1-host2', networks=2, az='az1')
        helpers.register_dhcp_agent('az2-host1', networks=3, az='az2')
        helpers.register_dhcp_agent('az2-host2', networks=4, az='az2')
        self.plugin.network_scheduler.schedule(self.plugin, self.ctx,
            {'id': '1111', 'availability_zone_hints': ['az2']})
        agents = self.plugin.get_dhcp_agents_hosting_networks(self.ctx,
                                                              ['1111'])
        self.assertEqual(1, len(agents))
        self.assertEqual('az2-host1', agents[0]['host'])

    def test_az_scheduler_default_az_hints(self):
        cfg.CONF.set_override('default_availability_zones', ['az1'])
        self._save_networks(['1111'])
        helpers.register_dhcp_agent('az1-host1', networks=1, az='az1')
        helpers.register_dhcp_agent('az1-host2', networks=2, az='az1')
        helpers.register_dhcp_agent('az2-host1', networks=3, az='az2')
        helpers.register_dhcp_agent('az2-host2', networks=4, az='az2')
        self.plugin.network_scheduler.schedule(self.plugin, self.ctx,
            {'id': '1111', 'availability_zone_hints': []})
        agents = self.plugin.get_dhcp_agents_hosting_networks(self.ctx,
                                                              ['1111'])
        self.assertEqual(1, len(agents))
        self.assertEqual('az1-host1', agents[0]['host'])

    def test_az_scheduler_two_az_hints(self):
        cfg.CONF.set_override('dhcp_agents_per_network', 2)
        self._save_networks(['1111'])
        helpers.register_dhcp_agent('az1-host1', networks=1, az='az1')
        helpers.register_dhcp_agent('az1-host2', networks=2, az='az1')
        helpers.register_dhcp_agent('az2-host1', networks=3, az='az2')
        helpers.register_dhcp_agent('az2-host2', networks=4, az='az2')
        helpers.register_dhcp_agent('az3-host1', networks=5, az='az3')
        helpers.register_dhcp_agent('az3-host2', networks=6, az='az3')
        self.plugin.network_scheduler.schedule(self.plugin, self.ctx,
            {'id': '1111', 'availability_zone_hints': ['az1', 'az3']})
        agents = self.plugin.get_dhcp_agents_hosting_networks(self.ctx,
                                                              ['1111'])
        self.assertEqual(2, len(agents))
        expected_hosts = set(['az1-host1', 'az3-host1'])
        hosts = set([a['host'] for a in agents])
        self.assertEqual(expected_hosts, hosts)

    def test_az_scheduler_two_az_hints_one_available_az(self):
        cfg.CONF.set_override('dhcp_agents_per_network', 2)
        self._save_networks(['1111'])
        helpers.register_dhcp_agent('az1-host1', networks=1, az='az1')
        helpers.register_dhcp_agent('az1-host2', networks=2, az='az1')
        helpers.register_dhcp_agent('az2-host1', networks=3, alive=False,
                                    az='az2')
        helpers.register_dhcp_agent('az2-host2', networks=4,
                                    admin_state_up=False, az='az2')
        self.plugin.network_scheduler.schedule(self.plugin, self.ctx,
            {'id': '1111', 'availability_zone_hints': ['az1', 'az2']})
        agents = self.plugin.get_dhcp_agents_hosting_networks(self.ctx,
                                                              ['1111'])
        self.assertEqual(2, len(agents))
        expected_hosts = set(['az1-host1', 'az1-host2'])
        hosts = set([a['host'] for a in agents])
        self.assertEqual(expected_hosts, hosts)

    def _test_az_scheduler_no_az_hints(self, multiple_agent=False):
        num_agent = 2 if multiple_agent else 1
        cfg.CONF.set_override('dhcp_agents_per_network', num_agent)
        self._save_networks(['1111'])
        helpers.register_dhcp_agent('az1-host1', networks=2, az='az1')
        helpers.register_dhcp_agent('az1-host2', networks=3, az='az1')
        helpers.register_dhcp_agent('az2-host1', networks=2, az='az2')
        helpers.register_dhcp_agent('az2-host2', networks=1, az='az2')
        self.plugin.network_scheduler.schedule(self.plugin, self.ctx,
            {'id': '1111', 'availability_zone_hints': []})
        agents = self.plugin.get_dhcp_agents_hosting_networks(self.ctx,
                                                              ['1111'])
        self.assertEqual(num_agent, len(agents))
        if multiple_agent:
            expected_hosts = set(['az1-host1', 'az2-host2'])
        else:
            expected_hosts = set(['az2-host2'])
        hosts = {a['host'] for a in agents}
        self.assertEqual(expected_hosts, hosts)

    def test_az_scheduler_no_az_hints_multiple_agent(self):
        self._test_az_scheduler_no_az_hints(multiple_agent=True)

    def test_az_scheduler_no_az_hints_one_agent(self):
        self._test_az_scheduler_no_az_hints()

    def test_az_scheduler_select_az_with_least_weight(self):
        self._save_networks(['1111'])
        dhcp_agents = []
        # Register 6 dhcp agents in 3 AZs, every AZ will have 2 agents.
        dhcp_agents.append(
            helpers.register_dhcp_agent('az1-host1', networks=6, az='az1'))
        dhcp_agents.append(
            helpers.register_dhcp_agent('az1-host2', networks=5, az='az1'))
        dhcp_agents.append(
            helpers.register_dhcp_agent('az2-host1', networks=4, az='az2'))
        dhcp_agents.append(
            helpers.register_dhcp_agent('az2-host2', networks=3, az='az2'))
        dhcp_agents.append(
            helpers.register_dhcp_agent('az3-host1', networks=2, az='az3'))
        dhcp_agents.append(
            helpers.register_dhcp_agent('az3-host2', networks=1, az='az3'))

        # Try multiple times to verify that the select of AZ scheduler will
        # output stably.
        for i in range(3):
            # Shuffle the agents
            random.shuffle(dhcp_agents)
            # Select agents with empty resource_hosted_agents. This means each
            # AZ will have same amount of agents scheduled (0 in this case)
            agents_select = self.plugin.network_scheduler.select(
                self.plugin, self.ctx, dhcp_agents, [], 2)

            self.assertEqual(2, len(agents_select))
            # The agent and az with least weight should always be selected
            # first
            self.assertEqual('az3-host2', agents_select[0]['host'])
            self.assertEqual('az3', agents_select[0]['availability_zone'])
            # The second selected agent should be the agent with least weight,
            # which is also not in the same az as the first selected agent.
            self.assertEqual('az2-host2', agents_select[1]['host'])
            self.assertEqual('az2', agents_select[1]['availability_zone'])
