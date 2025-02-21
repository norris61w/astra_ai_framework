import asyncio
from asyncio import Future
from typing import Optional, cast, List, Dict, Any, TYPE_CHECKING

from astracommon.connections.connection_type import ConnectionType
from astracommon.exceptions import FeedSubscriptionTimeoutError
from astracommon.feed.feed import FeedKey
from astracommon.messages.astra.abstract_astra_message import AbstractBloxrouteMessage
from astracommon.models.transaction_key import TransactionKey
from astracommon.rpc.external.eth_ws_subscriber import EthWsSubscriber
from astracommon.rpc.provider.abstract_ws_provider import WsException
from astracommon.services.transaction_service import TransactionService
from astracommon.utils import convert
from astracommon.utils.object_hash import Sha256Hash
from astracommon.feed.feed_manager import FeedManager
from astracommon.feed.feed_source import FeedSource
from astragateway import log_messages
from astracommon.feed.eth.eth_raw_transaction import EthRawTransaction
from astracommon.feed.eth.eth_pending_transaction_feed import EthPendingTransactionFeed
from astragateway.connections.gateway_connection import GatewayConnection
from astragateway.messages.gateway.confirmed_tx_message import ConfirmedTxMessage
from astragateway.utils.stats.transaction_feed_stats_service import transaction_feed_stats_service
from astrautils import logging

if TYPE_CHECKING:
    from astragateway.connections.eth.eth_gateway_node import EthGatewayNode

logger = logging.get_logger(__name__)

SUBSCRIBE_REQUEST_ID = "1"


class EthWsProxyPublisher(EthWsSubscriber):
    """
    Publishes transactions accepted to Ethereum mempool to a `pendingTxs` feed.
    """

    def __init__(
        self,
        ws_uri: str,
        feed_manager: FeedManager,
        transaction_service: TransactionService,
        node: "EthGatewayNode"
    ) -> None:
        self.feed_manager = feed_manager
        self.transaction_service = transaction_service
        self.node = node

        # ok, lifecycle patterns are a bit different
        super().__init__(ws_uri)
        self.receiving_tasks: List[Future] = []
        self.stream_confirmation_messages = node.opts.stream_to_peer_gateway is not None

    async def revive(self) -> None:
        """
        Revives subscriber; presumably, subscriber got disconnected earlier
        and stopped retrying.
        """
        if self.ws is None and not self.running:
            logger.info("Attempting to revive Ethereum websockets feed...")
            await self.reconnect()

    async def reconnect(self) -> None:
        logger.warning(log_messages.ETH_WS_SUBSCRIBER_CONNECTION_BROKEN)

        for receiving_task in self.receiving_tasks:
            receiving_task.cancel()
        self.receiving_tasks = []

        try:
            await super().reconnect()
        except ConnectionRefusedError:
            self.running = False

        if self.running:
            await self.subscribe_to_feeds()
            logger.info("Reconnected to Ethereum websocket feed")
        else:
            logger.warning(log_messages.ETH_RPC_COULD_NOT_RECONNECT)

    async def start(self) -> None:
        ws_uri = self.uri
        if ws_uri is not None:

            await self.initialize()

            logger.info("Subscribed to Ethereum websocket feed.")
            await self.subscribe_to_feeds()

    async def subscribe_to_feeds(self):
        subscription_id = await self.subscribe("newPendingTransactions")
        self.receiving_tasks.append(asyncio.create_task(self.handle_tx_notifications(subscription_id)))

        subscription_id = await self.subscribe("newHeads")
        self.receiving_tasks.append(asyncio.create_task(self.handle_block_notifications(subscription_id)))

    async def handle_tx_notifications(self, subscription_id: str) -> None:
        while self.running:
            next_notification = await self.get_next_subscription_notification_by_id(
                subscription_id
            )
            transaction_hash = next_notification.notification
            assert isinstance(transaction_hash, str)
            self.process_received_transaction(
                self.transaction_service.get_transaction_key(
                    Sha256Hash(
                        convert.hex_to_bytes(
                            transaction_hash[2:]
                        )
                    )
                )
            )

    async def handle_block_notifications(self, subscription_id: str) -> None:
        while self.running:
            next_notification = await self.get_next_subscription_notification_by_id_timeout(
                subscription_id
            )
            if not next_notification:
                raise FeedSubscriptionTimeoutError()
            logger.debug(
                "NewBlockHeader Notification {} from node", next_notification
            )
            block_header = next_notification.notification
            block_hash = Sha256Hash(convert.hex_to_bytes(block_header["hash"][2:]))
            block_number = int(block_header["number"], 16)
            block_difficulty = int(block_header["difficulty"], 16)

            self.node.publish_block(
                block_number, block_hash, None, FeedSource.BLOCKCHAIN_RPC
            )
            self.node.block_processing_service.set_last_confirmed_block_parameters(
                block_number, block_difficulty
            )

    def process_received_transaction(self, transaction_key: TransactionKey) -> None:
        tx_contents = cast(
            Optional[memoryview],
            self.transaction_service.get_transaction_by_key(transaction_key)
        )
        if tx_contents is None:
            asyncio.create_task(self.fetch_missing_transaction(transaction_key))
        else:
            self.process_transaction_with_contents(transaction_key, tx_contents)

    def process_transaction_with_contents(
        self, transaction_key: TransactionKey, tx_contents: memoryview
    ) -> None:
        transaction_feed_stats_service.log_pending_transaction_from_local(transaction_key.transaction_hash)

        self.feed_manager.publish_to_feed(
            FeedKey(EthPendingTransactionFeed.NAME, self.node.network_num),
            EthRawTransaction(
                transaction_key.transaction_hash,
                tx_contents,
                FeedSource.BLOCKCHAIN_RPC,
                local_region=True
            )
        )
        if self.stream_confirmation_messages:
            self.broadcast_confirmation_message(ConfirmedTxMessage(transaction_key.transaction_hash))

    async def fetch_missing_transaction(self, transaction_key: TransactionKey) -> None:
        try:
            response = await self.call_rpc(
                "eth_getTransactionByHash",
                [f"0x{str(transaction_key.transaction_hash)}"]
            )
            self.process_transaction_with_parsed_contents(transaction_key, response.result)
        except WsException:
            # ok, don't continue processing
            logger.debug(
                "Attempt to fetch transaction {} was interrupted by a broken connection. "
                "Abandoning.",
                transaction_key.transaction_hash
            )
            pass

    def process_transaction_with_parsed_contents(
        self, transaction_key: TransactionKey, parsed_tx: Optional[Dict[str, Any]]
    ) -> None:
        transaction_feed_stats_service.log_pending_transaction_from_local(transaction_key.transaction_hash)

        if parsed_tx is None:
            logger.debug(log_messages.TRANSACTION_NOT_FOUND_IN_MEMPOOL, transaction_key.transaction_hash)
            transaction_feed_stats_service.log_pending_transaction_missing_contents()
        else:
            if parsed_tx.get("type", "0x0") == "0x2":
                gas_price = int(parsed_tx["maxFeePerGas"], 16)
            else:
                gas_price = int(parsed_tx["gasPrice"], 16)
            if gas_price >= self.node.get_network_min_transaction_fee():
                self.feed_manager.publish_to_feed(
                    FeedKey(EthPendingTransactionFeed.NAME, network_num=self.node.network_num),
                    EthRawTransaction(
                        transaction_key.transaction_hash,
                        parsed_tx,
                        FeedSource.BLOCKCHAIN_RPC,
                        local_region=True,
                    )
                )

            if self.stream_confirmation_messages:
                self.broadcast_confirmation_message(ConfirmedTxMessage(transaction_key.transaction_hash))

    def broadcast_confirmation_message(self, message: AbstractBloxrouteMessage) -> None:
        gateway_connections = cast(
            List[GatewayConnection],
            self.node.connection_pool.get_by_connection_types((ConnectionType.EXTERNAL_GATEWAY,))
        )
        for connection in gateway_connections:
            if connection.stream_confirmation_messages:
                connection.enqueue_msg(message)

    async def stop(self) -> None:
        await self.close()
        for receiving_task in self.receiving_tasks:
            receiving_task.cancel()
