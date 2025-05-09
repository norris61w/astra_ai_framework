import asyncio
import blxr_rlp as rlp
from mock import MagicMock
from typing import Dict, Any, Iterator

from astracommon.rpc.provider.ws_provider import WsProvider
from astracommon import constants
from astracommon.feed.feed import FeedKey
from astracommon.messages.astra.tx_message import TxMessage
from astracommon.messages.eth.serializers.transaction import Transaction
from astracommon.models.bdn_service_model_base import FeedServiceModelBase
from astracommon.models.node_type import NodeType
from astracommon.models.outbound_peer_model import OutboundPeerModel
from astracommon.rpc.json_rpc_response import JsonRpcResponse
from astracommon.rpc.rpc_request_type import RpcRequestType
from astracommon.rpc import rpc_constants
from astracommon.services.threaded_request_service import ThreadedRequestService
from astracommon.test_utils import helpers
from astracommon.test_utils.abstract_test_case import AbstractTestCase
from astracommon.test_utils.helpers import async_test, AsyncMock
from astracommon.rpc.rpc_errors import RpcError
from astracommon.models.bdn_account_model_base import BdnAccountModelBase
from astracommon.models.bdn_service_model_config_base import BdnFeedServiceModelConfigBase
from astracommon.rpc.provider.abstract_ws_provider import WsException

from astracommon.feed.eth.eth_new_transaction_feed import EthNewTransactionFeed
from astracommon.feed.eth.eth_pending_transaction_feed import EthPendingTransactionFeed
from astracommon.feed.eth.eth_raw_transaction import EthRawTransaction
from astracommon.utils.object_hash import Sha256Hash
from astragateway.feed.eth.eth_on_block_feed import EthOnBlockFeed, EventNotification
from astracommon.feed.new_transaction_feed import (
    RawTransaction,
    RawTransactionFeedEntry,
    FeedSource,
)
from astragateway.feed.eth.eth_raw_block import EthRawBlock
from astragateway.feed.eth.eth_transaction_receipts_feed import EthTransactionReceiptsFeed
from astragateway.messages.eth.eth_normal_message_converter import (
    EthNormalMessageConverter,
)
from astragateway.messages.eth.internal_eth_block_info import InternalEthBlockInfo
from astragateway.messages.eth.protocol.new_block_eth_protocol_message import NewBlockEthProtocolMessage
from astragateway.messages.eth.protocol.transactions_eth_protocol_message import (
    TransactionsEthProtocolMessage,
)
from astragateway.rpc.ws.ws_server import WsServer
from astragateway.testing import gateway_helpers
from astragateway.testing.mocks import mock_eth_messages
from astragateway.testing.mocks.mock_gateway_node import MockGatewayNode
from astragateway.testing.mocks.mock_eth_ws_proxy_publisher import MockEthWsProxyPublisher
from astrautils import logging

logger = logging.get_logger(__name__)


def generate_new_eth_transaction() -> TxMessage:
    transaction = mock_eth_messages.get_dummy_transaction(1)
    transactions_eth_message = TransactionsEthProtocolMessage(None, [transaction])
    tx_message = EthNormalMessageConverter().tx_to_astra_txs(transactions_eth_message, 5)[
        0
    ][0]
    return tx_message


def generate_new_eth_with_to_transaction(to: str) -> TxMessage:
    transaction = mock_eth_messages.get_dummy_transaction(1, to_address_str=to)
    transactions_eth_message = TransactionsEthProtocolMessage(None, [transaction])
    tx_message = EthNormalMessageConverter().tx_to_astra_txs(transactions_eth_message, 5)[
        0
    ][0]
    return tx_message


def get_expected_eth_tx_contents(eth_tx_message: TxMessage) -> Dict[str, Any]:
    transaction = rlp.decode(eth_tx_message.tx_val().tobytes(), Transaction)
    expected_tx_contents = transaction.to_json()
    expected_tx_contents["gasPrice"] = expected_tx_contents["gas_price"]
    del expected_tx_contents["gas_price"]
    return expected_tx_contents


def get_block_message_lazy(
    block_message: InternalEthBlockInfo
) -> Iterator[InternalEthBlockInfo]:
    yield block_message


class WsProviderTest(AbstractTestCase):
    @async_test
    async def setUp(self) -> None:
        self.feed_service_model = FeedServiceModelBase(
            allow_filtering=True,
            available_fields=["all"]
        )
        self.base_feed_service_model = BdnFeedServiceModelConfigBase(
            expire_date="2999-01-01",
            feed=self.feed_service_model
        )
        account_model = BdnAccountModelBase(
            "account_id",
            "account_name",
            "fake_certificate",
            tier_name="Developer",
            new_transaction_streaming=self.base_feed_service_model,
            new_pending_transaction_streaming=self.base_feed_service_model,
            on_block_feed=self.base_feed_service_model,
            transaction_receipts_feed=self.base_feed_service_model
        )
        gateway_opts = gateway_helpers.get_gateway_opts(8000, ws=True)
        gateway_opts.set_account_options(account_model)

        self.gateway_node = MockGatewayNode(gateway_opts)
        self.gateway_node.NODE_TYPE = NodeType.INTERNAL_GATEWAY
        self.transaction_streamer_peer = OutboundPeerModel(
            "127.0.0.1", 8006, node_type=NodeType.INTERNAL_GATEWAY
        )
        self.gateway_node.requester = ThreadedRequestService(
            "mock_thread_service",
            self.gateway_node.alarm_queue,
            constants.THREADED_HTTP_POOL_SLEEP_INTERVAL_S,
        )
        self.gateway_node.requester.start()
        self.server = WsServer(
            constants.LOCALHOST, 8005, self.gateway_node.feed_manager, self.gateway_node
        )
        self.ws_uri = f"ws://{constants.LOCALHOST}:8005"

        await self.server.start()
        self.gateway_node.get_ws_server_status = MagicMock(return_value=True)

    @async_test
    async def test_eth_new_transactions_feed_default_subscribe(self):
        self.gateway_node.feed_manager.feeds.clear()
        self.gateway_node.feed_manager.register_feed(EthNewTransactionFeed(network_num=self.gateway_node.network_num))
        to = "0x1111111111111111111111111111111111111111"
        eth_tx_message = generate_new_eth_with_to_transaction(to[2:])
        eth_transaction = EthRawTransaction(
            eth_tx_message.tx_hash(),
            eth_tx_message.tx_val(),
            FeedSource.BLOCKCHAIN_SOCKET,
            local_region=True
        )
        expected_tx_hash = f"0x{str(eth_transaction.tx_hash)}"
        logger.error(expected_tx_hash)
        async with WsProvider(self.ws_uri) as ws:
            subscription_id = await ws.subscribe(
                "newTxs", options={"filters": f"to = {to} or to = aaaa"}
            )

            self.gateway_node.feed_manager.publish_to_feed(FeedKey("newTxs", self.gateway_node.network_num), eth_transaction)

            subscription_message = await ws.get_next_subscription_notification_by_id(
                subscription_id
            )
            self.assertEqual(subscription_id, subscription_message.subscription_id)
            self.assertEqual(
                expected_tx_hash, subscription_message.notification["txHash"]
            )

            expected_tx_contents = get_expected_eth_tx_contents(eth_tx_message)
            self.assertDictEqual(
                expected_tx_contents, subscription_message.notification["txContents"]
            )

            self.assertTrue(subscription_message.notification["localRegion"])

    @async_test
    async def test_eth_new_tx_feed_subscribe_include_from_blockchain(self):
        self.gateway_node.feed_manager.feeds.clear()
        self.gateway_node.feed_manager.register_feed(EthNewTransactionFeed(network_num=self.gateway_node.network_num))

        eth_tx_message = generate_new_eth_transaction()
        eth_transaction = EthRawTransaction(
            eth_tx_message.tx_hash(),
            eth_tx_message.tx_val(),
            FeedSource.BLOCKCHAIN_SOCKET,
            local_region=True
        )
        expected_tx_hash = f"0x{str(eth_transaction.tx_hash)}"

        async with WsProvider(self.ws_uri) as ws:
            subscription_id = await ws.subscribe(
                "newTxs", {"include_from_blockchain": True}
            )

            self.gateway_node.feed_manager.publish_to_feed(FeedKey("newTxs", self.gateway_node.network_num), eth_transaction)

            subscription_message = await ws.get_next_subscription_notification_by_id(
                subscription_id
            )
            self.assertEqual(subscription_id, subscription_message.subscription_id)
            self.assertEqual(
                expected_tx_hash, subscription_message.notification["txHash"]
            )

            expected_tx_contents = get_expected_eth_tx_contents(eth_tx_message)
            self.assertEqual(
                expected_tx_contents, subscription_message.notification["txContents"]
            )
            self.assertTrue(subscription_message.notification["localRegion"])

    @async_test
    async def test_eth_new_tx_feed_subscribe_not_include_from_blockchain(self):
        self.gateway_node.feed_manager.feeds.clear()
        self.gateway_node.feed_manager.register_feed(EthNewTransactionFeed(network_num=self.gateway_node.network_num))

        eth_tx_message = generate_new_eth_transaction()
        eth_transaction = EthRawTransaction(
            eth_tx_message.tx_hash(), eth_tx_message.tx_val(), FeedSource.BDN_SOCKET, local_region=True
        )
        expected_tx_hash = f"0x{str(eth_transaction.tx_hash)}"

        eth_tx_message_blockchain = generate_new_eth_transaction()
        eth_transaction_blockchain = EthRawTransaction(
            eth_tx_message_blockchain.tx_hash(),
            eth_tx_message_blockchain.tx_val(),
            FeedSource.BLOCKCHAIN_SOCKET,
            local_region=True
        )

        async with WsProvider(self.ws_uri) as ws:
            subscription_id = await ws.subscribe(
                "newTxs", {"include_from_blockchain": False}
            )

            self.gateway_node.feed_manager.publish_to_feed(FeedKey("newTxs", self.gateway_node.network_num), eth_transaction)
            subscription_message = await ws.get_next_subscription_notification_by_id(
                subscription_id
            )
            self.assertEqual(subscription_id, subscription_message.subscription_id)
            self.assertEqual(
                expected_tx_hash, subscription_message.notification["txHash"]
            )

            self.gateway_node.feed_manager.publish_to_feed(
                FeedKey("newTxs", self.gateway_node.network_num), eth_transaction_blockchain
            )
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(
                    ws.get_next_subscription_notification_by_id(subscription_id), 0.1
                )

    async def test_eth_pending_transactions_feed_default_subscribe(self):
        self.gateway_node.feed_manager.register_feed(
            EthPendingTransactionFeed(self.gateway_node.alarm_queue, network_num=self.gateway_node.network_num)
        )

        eth_tx_message = generate_new_eth_transaction()
        eth_transaction = EthRawTransaction(
            eth_tx_message.tx_hash(), eth_tx_message.tx_val(), FeedSource.BDN_SOCKET, local_region=True
        )
        expected_tx_hash = f"0x{str(eth_transaction.tx_hash)}"

        async with WsProvider(self.ws_uri) as ws:
            subscription_id = await ws.subscribe("pendingTxs")

            self.gateway_node.feed_manager.publish_to_feed(
                FeedKey("pendingTxs", self.gateway_node.network_num), eth_transaction
            )

            subscription_message = await ws.get_next_subscription_notification_by_id(
                subscription_id
            )
            self.assertEqual(subscription_id, subscription_message.subscription_id)
            self.assertEqual(
                expected_tx_hash, subscription_message.notification["txHash"]
            )

    @async_test
    async def test_eth_pending_tx_feed_subscribe_handles_no_duplicates(self):
        self.gateway_node.feed_manager.register_feed(
            EthPendingTransactionFeed(self.gateway_node.alarm_queue, network_num=self.gateway_node.network_num)
        )

        eth_tx_message = generate_new_eth_transaction()
        eth_transaction = EthRawTransaction(
            eth_tx_message.tx_hash(), eth_tx_message.tx_val(), FeedSource.BDN_SOCKET, local_region=True
        )
        expected_tx_hash = f"0x{str(eth_transaction.tx_hash)}"

        async with WsProvider(self.ws_uri) as ws:
            subscription_id = await ws.subscribe("pendingTxs", {"duplicates": False})

            self.gateway_node.feed_manager.publish_to_feed(
                FeedKey("pendingTxs", self.gateway_node.network_num), eth_transaction
            )

            subscription_message = await ws.get_next_subscription_notification_by_id(
                subscription_id
            )
            self.assertEqual(subscription_id, subscription_message.subscription_id)
            self.assertEqual(
                expected_tx_hash, subscription_message.notification["txHash"]
            )

            # will not publish twice
            self.gateway_node.feed_manager.publish_to_feed(
                FeedKey("pendingTxs", self.gateway_node.network_num), eth_transaction
            )
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(
                    ws.get_next_subscription_notification_by_id(subscription_id), 0.1
                )

    @async_test
    async def test_eth_pending_tx_feed_subscribe_with_duplicates(self):
        self.gateway_node.feed_manager.register_feed(
            EthPendingTransactionFeed(self.gateway_node.alarm_queue, network_num=self.gateway_node.network_num)
        )

        eth_tx_message = generate_new_eth_transaction()
        eth_transaction = EthRawTransaction(
            eth_tx_message.tx_hash(), eth_tx_message.tx_val(), FeedSource.BDN_SOCKET, local_region=True
        )
        expected_tx_hash = f"0x{str(eth_transaction.tx_hash)}"

        async with WsProvider(self.ws_uri) as ws:
            subscription_id = await ws.subscribe("pendingTxs", {"duplicates": True})

            self.gateway_node.feed_manager.publish_to_feed(
                FeedKey("pendingTxs", self.gateway_node.network_num), eth_transaction
            )

            subscription_message = await ws.get_next_subscription_notification_by_id(
                subscription_id
            )
            self.assertEqual(subscription_id, subscription_message.subscription_id)
            self.assertEqual(
                expected_tx_hash, subscription_message.notification["txHash"]
            )

            # will publish twice
            self.gateway_node.feed_manager.publish_to_feed(
                FeedKey("pendingTxs", self.gateway_node.network_num), eth_transaction
            )
            subscription_message = await ws.get_next_subscription_notification_by_id(
                subscription_id
            )
            self.assertEqual(subscription_id, subscription_message.subscription_id)
            self.assertEqual(
                expected_tx_hash, subscription_message.notification["txHash"]
            )

    @async_test
    async def test_onblock_feed_default_subscribe(self):
        self.gateway_node.opts.eth_ws_uri = f"ws://{constants.LOCALHOST}:8005"
        block_height = 100
        name = "abc123"

        self.gateway_node.feed_manager.register_feed(EthOnBlockFeed(self.gateway_node, network_num=self.gateway_node.network_num))
        self.gateway_node.eth_ws_proxy_publisher = MockEthWsProxyPublisher(
            "", None, None, self.gateway_node
        )
        self.gateway_node.eth_ws_proxy_publisher.call_rpc = AsyncMock(
            return_value=JsonRpcResponse(request_id=1)
        )

        async with WsProvider(self.ws_uri) as ws:
            subscription_id = await ws.subscribe(
                rpc_constants.ETH_ON_BLOCK_FEED_NAME,
                {"call_params": [{"data": "0x", "name": name}]},
            )

            self.gateway_node.feed_manager.publish_to_feed(
                FeedKey(rpc_constants.ETH_ON_BLOCK_FEED_NAME, self.gateway_node.network_num),
                EventNotification(block_height=block_height),
            )

            subscription_message = await ws.get_next_subscription_notification_by_id(
                subscription_id
            )
            self.assertEqual(subscription_id, subscription_message.subscription_id)
            print(subscription_message)
            self.assertEqual(
                block_height, subscription_message.notification["blockHeight"]
            )

            self.assertEqual(name, subscription_message.notification["name"])

    @async_test
    async def test_eth_transaction_receipts_feed_default_subscribe(self):
        self.gateway_node.opts.eth_ws_uri = f"ws://{constants.LOCALHOST}:8005"
        self.gateway_node.feed_manager.register_feed(
            EthTransactionReceiptsFeed(self.gateway_node, network_num=self.gateway_node.network_num)
        )
        self.gateway_node.eth_ws_proxy_publisher = MockEthWsProxyPublisher(
            "", None, None, self.gateway_node
        )
        block_hash = Sha256Hash.generate_object_hash()
        receipt_result = {
            "blockHash":block_hash.to_string(True),"blockNumber":"0xaf25e5","cumulativeGasUsed":"0xbdb9ae","from":"0x82170dd1cec50107963bf1ba1e80955ea302c5ce","gasUsed":"0x5208","logs":[],"logsBloom":"0x00000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000","status":"0x1","to":"0xa09f63d9a0b0fbe89e41e51282ad660e7c876165","transactionHash":"0xbcdc5b22bf463f9b8766dd61cc133caf13472b6ae8474061134d9dc2983625f6","transactionIndex":"0x90"
        }
        self.gateway_node.eth_ws_proxy_publisher.call_rpc = AsyncMock(
            return_value=JsonRpcResponse(
                request_id=1, result=receipt_result
            )
        )
        block = mock_eth_messages.get_dummy_block(5)
        internal_block_info = InternalEthBlockInfo.from_new_block_msg(NewBlockEthProtocolMessage(None, block, 1))
        eth_raw_block_1 = EthRawBlock(
            1,
            block_hash,
            FeedSource.BLOCKCHAIN_RPC,
            get_block_message_lazy(None)
        )
        eth_raw_block_2 = EthRawBlock(
            1,
            block_hash,
            FeedSource.BLOCKCHAIN_SOCKET,
            get_block_message_lazy(internal_block_info)
        )

        async with WsProvider(self.ws_uri) as ws:
            subscription_id = await ws.subscribe(rpc_constants.ETH_TRANSACTION_RECEIPTS_FEED_NAME)

            self.gateway_node.feed_manager.publish_to_feed(
                FeedKey(rpc_constants.ETH_TRANSACTION_RECEIPTS_FEED_NAME, network_num=self.gateway_node.network_num),
                eth_raw_block_1
            )
            self.gateway_node.feed_manager.publish_to_feed(
                FeedKey(rpc_constants.ETH_TRANSACTION_RECEIPTS_FEED_NAME, network_num=self.gateway_node.network_num),
                eth_raw_block_2
            )

            for i in range(len(block.transactions)):
                subscription_message = await ws.get_next_subscription_notification_by_id(
                    subscription_id
                )
                self.assertEqual(subscription_id, subscription_message.subscription_id)
                self.assertEqual(subscription_message.notification, {"receipt": receipt_result})

    @async_test
    async def test_eth_transaction_receipts_feed_specify_include(self):
        self.gateway_node.opts.eth_ws_uri = f"ws://{constants.LOCALHOST}:8005"
        self.gateway_node.feed_manager.register_feed(
            EthTransactionReceiptsFeed(self.gateway_node, network_num=self.gateway_node.network_num)
        )
        self.gateway_node.eth_ws_proxy_publisher = MockEthWsProxyPublisher(
            "", None, None, self.gateway_node
        )
        block_hash = Sha256Hash.generate_object_hash()
        receipt_response = {
            "blockHash":block_hash.to_string(True),"blockNumber":"0xaf25e5","cumulativeGasUsed":"0xbdb9ae","from":"0x82170dd1cec50107963bf1ba1e80955ea302c5ce","gasUsed":"0x5208","logs":[],"logsBloom":"0x00000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000","status":"0x1","to":"0xa09f63d9a0b0fbe89e41e51282ad660e7c876165","transactionHash":"0xbcdc5b22bf463f9b8766dd61cc133caf13472b6ae8474061134d9dc2983625f6","transactionIndex":"0x90"
        }
        receipt_result = {
           "transactionHash":"0xbcdc5b22bf463f9b8766dd61cc133caf13472b6ae8474061134d9dc2983625f6"
        }
        self.gateway_node.eth_ws_proxy_publisher.call_rpc = AsyncMock(
            return_value=JsonRpcResponse(
                request_id=1, result=receipt_response
            )
        )
        block = mock_eth_messages.get_dummy_block(5)
        internal_block_info = InternalEthBlockInfo.from_new_block_msg(NewBlockEthProtocolMessage(None, block, 1))
        eth_raw_block = EthRawBlock(
            1,
            block_hash,
            FeedSource.BLOCKCHAIN_RPC,
            get_block_message_lazy(internal_block_info)
        )

        async with WsProvider(self.ws_uri) as ws:
            subscription_id = await ws.subscribe(
                rpc_constants.ETH_TRANSACTION_RECEIPTS_FEED_NAME,
                {"include": ["receipt.transaction_hash"]}
            )

            self.gateway_node.feed_manager.publish_to_feed(
                FeedKey(rpc_constants.ETH_TRANSACTION_RECEIPTS_FEED_NAME, network_num=self.gateway_node.network_num),
                eth_raw_block
            )

            for i in range(len(block.transactions)):
                subscription_message = await ws.get_next_subscription_notification_by_id(
                    subscription_id
                )
                self.assertEqual(subscription_id, subscription_message.subscription_id)
                self.assertEqual(subscription_message.notification, {"receipt": receipt_result})

    @async_test
    async def test_connection_and_close(self):
        async with WsProvider(self.ws_uri) as ws:
            self.assertTrue(ws.running)
            await self.server._connections[0].close()

    @async_test
    async def test_connection_and_close_unexpectedly(self):
        async with WsProvider(self.ws_uri) as ws:
            self.assertEqual(1, len(self.server._connections))

            connection_handler = self.server._connections[0]
            connection_handler.rpc_handler.handle_request = AsyncMock(
                side_effect=connection_handler.close
            )

            with self.assertRaises(WsException):
                await ws.subscribe("newTxs")

    @async_test
    async def test_connection_and_close_while_receiving_subscriptions(self):
        async with WsProvider(self.ws_uri) as ws:
            self.assertEqual(1, len(self.server._connections))

            connection_handler = self.server._connections[0]
            self.assertTrue(ws.running)
            subscription_id = await ws.subscribe("newTxs")

            tx_contents = helpers.generate_bytes(250)

            raw_published_message = RawTransaction(
                helpers.generate_object_hash(),
                memoryview(tx_contents),
                FeedSource.BDN_SOCKET,
                False
            )
            serialized_published_message = RawTransactionFeedEntry(
                raw_published_message.tx_hash, raw_published_message.tx_contents, raw_published_message.local_region
            )
            self.gateway_node.feed_manager.publish_to_feed(
                FeedKey("newTxs", network_num=self.gateway_node.network_num), raw_published_message
            )

            subscription_message = await ws.get_next_subscription_notification_by_id(
                subscription_id
            )
            self.assertEqual(subscription_id, subscription_message.subscription_id)

            self.assertEqual(
                serialized_published_message.tx_hash,
                subscription_message.notification["txHash"],
            )
            self.assertEqual(
                serialized_published_message.tx_contents,
                subscription_message.notification["txContents"],
            )

            self.assertFalse(
                subscription_message.notification["localRegion"],
            )

            task = asyncio.create_task(
                ws.get_next_subscription_notification_by_id(subscription_id)
            )
            await connection_handler.close()
            await asyncio.sleep(0.01)

            exception = task.exception()
            self.assertIsInstance(exception, WsException)

    @async_test
    async def test_connection_to_invalid_channel(self):
        async with WsProvider(self.ws_uri) as ws:
            self.assertEqual(1, len(self.server._connections))

            connection_handler = self.server._connections[0]
            with self.assertRaises(RpcError):
                _ = await ws.subscribe("fake_channel")
            await connection_handler.close()

    @async_test
    async def test_multiple_rpc_calls_mixed_response(self):
        # unlikely to ever actually happen in practice, but should be handled
        async with WsProvider(self.ws_uri) as ws:
            self.assertEqual(1, len(self.server._connections))

            connection_handler = self.server._connections[0]
            connection_handler.rpc_handler.handle_request = AsyncMock()

            subscribe_task_1 = asyncio.create_task(
                ws.call_astra(RpcRequestType.SUBSCRIBE, ["newTxs"], request_id="123")
            )
            await asyncio.sleep(0)
            subscribe_task_2 = asyncio.create_task(
                ws.call_astra(RpcRequestType.SUBSCRIBE, ["newTxs"], request_id="124")
            )

            server_ws = connection_handler.ws
            assert server_ws is not None

            # send responses out of order
            await server_ws.send(JsonRpcResponse("124", "subid2").to_jsons())
            await server_ws.send(JsonRpcResponse("123", "subid1").to_jsons())

            await asyncio.sleep(0.01)
            self.assertTrue(subscribe_task_1.done())
            self.assertTrue(subscribe_task_2.done())

            subscription_1 = subscribe_task_1.result()
            self.assertEqual("123", subscription_1.id)
            self.assertEqual("subid1", subscription_1.result)

            subscription_2 = subscribe_task_2.result()
            self.assertEqual("subid2", subscription_2.result)

    @async_test
    async def test_eth_new_transactions_feed_subscribe_filters(self):
        self.gateway_node.feed_manager.feeds.clear()
        self.gateway_node.feed_manager.register_feed(EthNewTransactionFeed(network_num=self.gateway_node.network_num))
        to = "0x1111111111111111111111111111111111111111"
        eth_tx_message = generate_new_eth_with_to_transaction(to[2:])
        eth_transaction = EthRawTransaction(
            eth_tx_message.tx_hash(),
            eth_tx_message.tx_val(),
            FeedSource.BLOCKCHAIN_SOCKET,
            local_region=True
        )
        expected_tx_hash = f"0x{str(eth_transaction.tx_hash)}"
        logger.error(expected_tx_hash)
        async with WsProvider(self.ws_uri) as ws:
            subscription_id = await ws.subscribe(
                "newTxs", options={"filters": f"to = {to} or to = aaaa"}
            )

            self.gateway_node.feed_manager.publish_to_feed(
                FeedKey("newTxs", network_num=self.gateway_node.network_num), eth_transaction
            )

            subscription_message = await ws.get_next_subscription_notification_by_id(
                subscription_id
            )
            self.assertEqual(subscription_id, subscription_message.subscription_id)
            self.assertEqual(
                expected_tx_hash, subscription_message.notification["txHash"]
            )

            expected_tx_contents = get_expected_eth_tx_contents(eth_tx_message)
            self.assertEqual(
                expected_tx_contents, subscription_message.notification["txContents"]
            )

    @async_test
    async def test_eth_new_transactions_feed_subscribe_filters2(self):
        self.gateway_node.feed_manager.feeds.clear()
        self.gateway_node.feed_manager.register_feed(EthNewTransactionFeed(network_num=self.gateway_node.network_num))
        to = "0x1111111111111111111111111111111111111112"
        eth_tx_message = generate_new_eth_with_to_transaction(to[2:])
        eth_transaction = EthRawTransaction(
            eth_tx_message.tx_hash(),
            eth_tx_message.tx_val(),
            FeedSource.BLOCKCHAIN_SOCKET,
            local_region=True
        )
        expected_tx_hash = f"0x{str(eth_transaction.tx_hash)}"
        to2 = "0x1111111111111111111111111111111111111111"
        eth_tx_message2 = generate_new_eth_with_to_transaction(to2[2:])
        eth_transaction2 = EthRawTransaction(
            eth_tx_message2.tx_hash(),
            eth_tx_message2.tx_val(),
            FeedSource.BLOCKCHAIN_SOCKET,
            local_region=True
        )
        expected_tx_hash2 = f"0x{str(eth_transaction2.tx_hash)}"
        logger.error(expected_tx_hash2)
        async with WsProvider(self.ws_uri) as ws:
            subscription_id = await ws.subscribe(
                "newTxs",
                options={
                    "filters": f"to = 0x1111111111111111111111111111111111111111 or to = aaaa"
                },
            )

            self.gateway_node.feed_manager.publish_to_feed(
                FeedKey("newTxs", network_num=self.gateway_node.network_num), eth_transaction
            )
            self.gateway_node.feed_manager.publish_to_feed(
                FeedKey("newTxs", network_num=self.gateway_node.network_num), eth_transaction2
            )

            subscription_message = await ws.get_next_subscription_notification_by_id(
                subscription_id
            )
            self.assertEqual(subscription_id, subscription_message.subscription_id)
            self.assertEqual(
                expected_tx_hash2, subscription_message.notification["txHash"]
            )

            expected_tx_contents = get_expected_eth_tx_contents(eth_tx_message2)
            self.assertEqual(
                expected_tx_contents, subscription_message.notification["txContents"]
            )

    @async_test
    async def test_eth_pending_transactions_feed_subscribe_filters3(self):
        self.gateway_node.feed_manager.feeds.clear()
        self.gateway_node.feed_manager.register_feed(
            EthPendingTransactionFeed(self.gateway_node.alarm_queue, network_num=self.gateway_node.network_num)
        )
        to = "0x1111111111111111111111111111111111111112"
        eth_tx_message = generate_new_eth_with_to_transaction(to[2:])
        eth_transaction = EthRawTransaction(
            eth_tx_message.tx_hash(),
            eth_tx_message.tx_val(),
            FeedSource.BLOCKCHAIN_SOCKET,
            local_region=True,
        )
        expected_tx_hash = f"0x{str(eth_transaction.tx_hash)}"
        to2 = "0x1111111111111111111111111111111111111111"
        eth_tx_message2 = generate_new_eth_with_to_transaction(to2[2:])
        eth_transaction2 = EthRawTransaction(
            eth_tx_message2.tx_hash(),
            eth_tx_message2.tx_val(),
            FeedSource.BLOCKCHAIN_SOCKET,
            local_region=True
        )
        expected_tx_hash2 = f"0x{str(eth_transaction2.tx_hash)}"
        logger.error(expected_tx_hash2)
        async with WsProvider(self.ws_uri) as ws:
            subscription_id = await ws.subscribe(
                "pendingTxs",
                options={
                    "filters": f"to in [0x1111111111111111111111111111111111111111, aaaa]"
                },
            )

            self.gateway_node.feed_manager.publish_to_feed(
                FeedKey("pendingTxs", network_num=self.gateway_node.network_num), eth_transaction
            )
            self.gateway_node.feed_manager.publish_to_feed(
                FeedKey("pendingTxs", network_num=self.gateway_node.network_num), eth_transaction2
            )

            subscription_message = await ws.get_next_subscription_notification_by_id(
                subscription_id
            )
            self.assertEqual(subscription_id, subscription_message.subscription_id)
            self.assertEqual(
                expected_tx_hash2, subscription_message.notification["txHash"]
            )

            expected_tx_contents = get_expected_eth_tx_contents(eth_tx_message2)
            self.assertEqual(
                expected_tx_contents, subscription_message.notification["txContents"]
            )

    @async_test
    async def test_eth_pending_transactions_feed_subscribe_filters4(self):
        self.gateway_node.feed_manager.feeds.clear()
        self.gateway_node.feed_manager.register_feed(
            EthPendingTransactionFeed(self.gateway_node.alarm_queue, network_num=self.gateway_node.network_num)
        )
        to = "0x"
        eth_tx_message = generate_new_eth_with_to_transaction(to[2:])
        eth_transaction = EthRawTransaction(
            eth_tx_message.tx_hash(),
            eth_tx_message.tx_val(),
            FeedSource.BLOCKCHAIN_SOCKET,
            local_region=True
        )
        expected_tx_hash = f"0x{str(eth_transaction.tx_hash)}"
        logger.error(expected_tx_hash)
        to2 = "0x0000000000000000000000000000000000000000"
        eth_tx_message2 = generate_new_eth_with_to_transaction(to2[2:])
        eth_transaction2 = EthRawTransaction(
            eth_tx_message2.tx_hash(),
            eth_tx_message2.tx_val(),
            FeedSource.BLOCKCHAIN_SOCKET,
            local_region=True
        )
        expected_tx_hash2 = f"0x{str(eth_transaction2.tx_hash)}"
        logger.error(expected_tx_hash2)
        async with WsProvider(self.ws_uri) as ws:
            subscription_id = await ws.subscribe(
                "pendingTxs",
                options={
                    "filters": f"to in [0x0000000000000000000000000000000000000000]"
                },
            )

            self.gateway_node.feed_manager.publish_to_feed(
                FeedKey("pendingTxs", network_num=self.gateway_node.network_num), eth_transaction
            )
            self.gateway_node.feed_manager.publish_to_feed(
                FeedKey("pendingTxs", network_num=self.gateway_node.network_num), eth_transaction2
            )

            subscription_message = await ws.get_next_subscription_notification_by_id(
                subscription_id
            )
            self.assertEqual(subscription_id, subscription_message.subscription_id)
            self.assertEqual(
                expected_tx_hash, subscription_message.notification["txHash"]
            )
            subscription_message = await ws.get_next_subscription_notification_by_id(
                subscription_id
            )
            self.assertEqual(subscription_id, subscription_message.subscription_id)
            self.assertEqual(
                expected_tx_hash2, subscription_message.notification["txHash"]
            )

    @async_test
    async def tearDown(self) -> None:
        await self.server.stop()
