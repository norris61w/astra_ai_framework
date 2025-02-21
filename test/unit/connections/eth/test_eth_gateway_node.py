from unittest import skip

from mock import MagicMock

from astracommon.models.node_type import NodeType
from astracommon.models.outbound_peer_model import OutboundPeerModel
from astracommon.network.ip_endpoint import IpEndpoint
from astracommon.network.socket_connection_state import SocketConnectionStates
from astracommon.network.transport_layer_protocol import TransportLayerProtocol
from astracommon.test_utils import helpers
from astracommon.test_utils.abstract_test_case import AbstractTestCase
from astracommon.test_utils.mocks.mock_node_ssl_service import MockNodeSSLService
from astracommon.test_utils.mocks.mock_socket_connection import MockSocketConnection
from astracommon.utils import convert
from astracommon.utils.blockchain_utils.eth import crypto_utils, eth_common_constants
from astragateway.connections.eth.eth_gateway_node import EthGatewayNode
from astragateway.connections.eth.eth_node_connection import EthNodeConnection
from astragateway.connections.eth.eth_node_discovery_connection import EthNodeDiscoveryConnection
from astragateway.testing import gateway_helpers


class EthGatewayNodeTest(AbstractTestCase):

    @skip("We now require that the public key is always specified, so this test no longer applies")
    def test_get_outbound_peer_addresses__initiate_handshake(self):
        self._test_get_outbound_peer_addresses(True, TransportLayerProtocol.UDP)

    def test_get_outbound_peer_addresses__no_initiate_handshake(self):
        self._test_get_outbound_peer_addresses(False, TransportLayerProtocol.TCP)

    def test_get_gateway_connection_class__do_not_initiate_handshake(self):
        node = self._set_up_test_node(False, generate_pub_key=True)
        connection = node.build_blockchain_connection(
            MockSocketConnection(1, node, ip_address=self.blockchain_ip, port=self.blockchain_port)
        )
        self.assertIsInstance(connection, EthNodeConnection)

    @skip("We now require that the public key is always specified, so this test no longer applies")
    def test_get_gateway_connection_class__initiate_handshake_no_remote_pub_key(self):
        node = self._set_up_test_node(True, generate_pub_key=True)
        connection = node.build_blockchain_connection(
            MockSocketConnection(1, node, ip_address=self.blockchain_ip, port=self.blockchain_port)
        )
        self.assertIsInstance(connection, EthNodeDiscoveryConnection)

    def test_get_gateway_connection_class__initiate_handshake_with_remote_pub_key(self):
        dummy_con_fileno = 123
        dummy_con_ip = "0.0.0.0"
        dummy_con_port = 12345
        node = self._set_up_test_node(True, generate_pub_key=True)
        node_public_key = self._get_dummy_public_key()
        discovery_connection = EthNodeDiscoveryConnection(
            MockSocketConnection(dummy_con_fileno, node, ip_address=dummy_con_ip,
                                 port=dummy_con_port), node
        )
        node.connection_pool.add(dummy_con_fileno, dummy_con_ip, dummy_con_port, discovery_connection)
        node.set_node_public_key(discovery_connection, node_public_key)
        # connection_cls = node.build_connection(, self.blockchain_ip, self.blockchain_port
        connection = node.build_blockchain_connection(
            MockSocketConnection(1, node, ip_address=self.blockchain_ip, port=self.blockchain_port)
        )
        self.assertIsInstance(connection, EthNodeConnection)

    def test_get_private_key(self):
        node = self._set_up_test_node(False, generate_pub_key=True)
        private_key = node.get_private_key()
        self.assertTrue(private_key)
        self.assertEqual(len(private_key), eth_common_constants.PRIVATE_KEY_LEN)

    def test_get_public_key(self):
        node = self._set_up_test_node(False, generate_pub_key=True)
        public_key = node.get_public_key()
        self.assertTrue(public_key)
        self.assertEqual(len(public_key), eth_common_constants.PUBLIC_KEY_LEN)

    def test_get_node_public_key__default(self):
        node = self._set_up_test_node(False, generate_pub_key=True)
        node_public_key = node.get_node_public_key(self.blockchain_ip, self.blockchain_port)
        self.assertIsNotNone(node_public_key)

    def test_set_node_public_key(self):
        dummy_con_fileno = 123
        dummy_con_ip = "0.0.0.0"
        dummy_con_port = 12345
        node = self._set_up_test_node(False, generate_pub_key=True)
        discovery_connection = EthNodeDiscoveryConnection(
            MockSocketConnection(dummy_con_fileno, node, ip_address=dummy_con_ip,
                                 port=dummy_con_port), node
        )
        node.connection_pool.add(dummy_con_fileno, dummy_con_ip, dummy_con_port, discovery_connection)
        self.assertEqual(1, len(self.node.connection_pool))

        node_public_key = node.get_node_public_key(self.blockchain_ip, self.blockchain_port)
        self.assertIsNotNone(node_public_key)

        new_node_public_key = self._get_dummy_public_key()
        node.set_node_public_key(discovery_connection, new_node_public_key)

        self.assertFalse(discovery_connection.socket_connection is not None and discovery_connection.socket_connection.alive)
        self.assertTrue(discovery_connection.socket_connection is None or SocketConnectionStates.DO_NOT_RETRY in discovery_connection.socket_connection.state)

        updated_node_public_key = node.get_node_public_key(self.blockchain_ip, self.blockchain_port)
        self.assertIsNotNone(updated_node_public_key)

    def _test_get_outbound_peer_addresses(self, initiate_handshake, expected_node_con_protocol):
        node = self._set_up_test_node(initiate_handshake, generate_pub_key=True)
        assert isinstance(node, EthGatewayNode)

        peer_connections = node.get_outbound_peer_info()
        self.assertTrue(peer_connections)

        self.assertEqual(len(self.servers) + 1, len(peer_connections))

        for server in self.servers:
            peer_connection = [peer for peer in peer_connections
                               if peer.endpoint.ip_address == server.ip and peer.endpoint.port == server.port][0]

            self.assertEqual(IpEndpoint(server.ip, server.port), peer_connection.endpoint)

        blockchain_connection = peer_connections[len(self.servers)]

        self.assertEqual(IpEndpoint(self.blockchain_ip, self.blockchain_port), blockchain_connection.endpoint)
        self.assertEqual(expected_node_con_protocol, blockchain_connection.transport_protocol)

    def _set_up_test_node(self, initialize_handshake, generate_pub_key=False):
        # Dummy address
        self.server_ip = "127.0.0.1"
        self.server_port = 1234

        self.blockchain_ip = "0.0.0.0"
        self.blockchain_port = 30303

        # Setting up dummy server addresses
        self.servers = [
            OutboundPeerModel("172.0.0.1", 2222, node_type=NodeType.GATEWAY),
            OutboundPeerModel("172.0.0.2", 3333, node_type=NodeType.GATEWAY),
            OutboundPeerModel("172.0.0.3", 4444, node_type=NodeType.GATEWAY)
        ]
        if generate_pub_key:
            pub_key = convert.bytes_to_hex(self._get_dummy_public_key())
        else:
            pub_key = None
        opts = gateway_helpers.get_gateway_opts(
            self.server_port,
            sid_expire_time=0,
            external_ip=self.server_ip,
            test_mode=[],
            peer_gateways=[],
            peer_relays=self.servers,
            blockchain_address=(self.blockchain_ip, self.blockchain_port),
            include_default_eth_args=True,
            pub_key=pub_key,
            no_discovery=not initialize_handshake
        )
        if opts.use_extensions:
            helpers.set_extensions_parallelism()
        node_ssl_service = MockNodeSSLService(EthGatewayNode.NODE_TYPE, MagicMock())
        self.node = EthGatewayNode(opts, node_ssl_service)
        self.node.requester = MagicMock()
        self.assertTrue(self.node)

        return self.node

    def _get_dummy_public_key(self):
        dummy_private_key = crypto_utils.make_private_key(helpers.generate_bytearray(111))
        return crypto_utils.private_to_public_key(dummy_private_key)
