#region IMPORTS

import logging
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    RunContext,
    TurnHandlingOptions,
    cli,
    function_tool,
    inference,
    llm,
    mcp,
    room_io,
)
from livekit.plugins import ai_coustics

from agent_instructions import NOVACART_AGENT_INSTRUCTIONS

#endregion


#region PROJECT CONFIGURATION

PROJECT_ROOT = Path(__file__).resolve().parent

CONSUMER_DATABASE = PROJECT_ROOT / "novacart_consumers.db"
SUPPORT_DATABASE = PROJECT_ROOT / "novacart_support.db"
KNOWLEDGE_BASE_DIR = PROJECT_ROOT / "knowledge_base"

load_dotenv(PROJECT_ROOT / ".env.local")

#endregion


#region LOGGING

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("novacart-agent")

#endregion


#region SESSION STATE

@dataclass
class SessionState:
    call_id: str
    consumer_id: str | None = None
    consumer_name: str | None = None
    verified: bool = False
    current_order_id: str | None = None
    escalation_risk: str | None = None
    created_ticket_id: str | None = None
    outcome_recorded: bool = False

#endregion


#region DATABASE CONNECTIONS

def open_consumer_database() -> sqlite3.Connection:
    """Open the existing customer/order database."""

    if not CONSUMER_DATABASE.exists():
        raise FileNotFoundError(
            f"Consumer database not found: {CONSUMER_DATABASE}"
        )

    connection = sqlite3.connect(CONSUMER_DATABASE)
    connection.row_factory = sqlite3.Row
    return connection


def open_support_database() -> sqlite3.Connection:
    """Open or create the runtime support database."""

    connection = sqlite3.connect(SUPPORT_DATABASE)
    connection.row_factory = sqlite3.Row
    return connection

#endregion


#region DATABASE VALIDATION

def validate_consumer_database() -> None:
    """Check that the consumer database has the required structure."""

    expected_columns = {
        "consumer_id",
        "consumer_name",
        "order_id",
        "order_status",
        "order_date",
        "order_time",
        "ordered_items",
        "order_price",
        "mode_of_payment",
    }

    connection = open_consumer_database()

    try:
        table_info = connection.execute(
            "PRAGMA table_info(consumer_orders)"
        ).fetchall()

        if not table_info:
            raise RuntimeError("Table 'consumer_orders' was not found.")

        actual_columns = {row["name"] for row in table_info}
        missing_columns = expected_columns - actual_columns

        if missing_columns:
            raise RuntimeError(
                "Missing database columns: "
                + ", ".join(sorted(missing_columns))
            )

        logger.info("Consumer database validation successful.")

    finally:
        connection.close()

#endregion


#region SUPPORT DATABASE

def initialize_support_database() -> None:
    """Create empty tables for tickets and call outcomes."""

    connection = open_support_database()

    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS support_tickets (
                ticket_id TEXT PRIMARY KEY,
                consumer_id TEXT NOT NULL,
                order_id TEXT,
                issue_type TEXT NOT NULL,
                issue_summary TEXT NOT NULL,
                priority TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS call_outcomes (
                call_id TEXT PRIMARY KEY,
                consumer_id TEXT,
                order_id TEXT,
                intent TEXT,
                sentiment TEXT,
                escalation_risk TEXT,
                ticket_id TEXT,
                resolution TEXT,
                created_at TEXT NOT NULL
            )
            """
        )

        connection.commit()
        logger.info("Support database initialized successfully.")

    finally:
        connection.close()
        
        

#endregion


#region KNOWLEDGE BASE VALIDATION

def validate_knowledge_base() -> None:
    """Check that all required policy documents exist."""

    required_files = {
        "delivery_delay_policy.md",
        "refund_policy.md",
        "cancellation_policy.md",
        "support_escalation_policy.md",
        "faq.md",
    }

    if not KNOWLEDGE_BASE_DIR.exists():
        raise FileNotFoundError(
            f"Knowledge-base folder not found: {KNOWLEDGE_BASE_DIR}"
        )

    missing_files = [
        filename
        for filename in required_files
        if not (KNOWLEDGE_BASE_DIR / filename).exists()
    ]

    if missing_files:
        raise FileNotFoundError(
            "Missing knowledge-base files: "
            + ", ".join(sorted(missing_files))
        )

    logger.info("Knowledge-base validation successful.")

#endregion


#region MCP KNOWLEDGE BASE

def build_knowledge_base_mcp() -> mcp.MCPToolset:
    """
    Connect the NovaCart policy folder through the
    Model Context Protocol filesystem server.
    """

    if os.name == "nt":
        command = os.environ.get("COMSPEC", "cmd.exe")
        args = [
            "/c",
            "npx",
            "-y",
            "@modelcontextprotocol/server-filesystem",
            str(KNOWLEDGE_BASE_DIR),
        ]
    else:
        command = "npx"
        args = [
            "-y",
            "@modelcontextprotocol/server-filesystem",
            str(KNOWLEDGE_BASE_DIR),
        ]

    return mcp.MCPToolset(
        id="novacart-knowledge-base",
        mcp_server=mcp.MCPServerStdio(
            command=command,
            args=args,
            env={**os.environ},
        ),
    )

#endregion


#region TOOL LATENCY

def log_tool_latency(
    tool_name: str,
    started_at: float,
    success: bool = True,
) -> None:
    """Log how long a backend/tool operation takes."""

    elapsed_ms = (time.perf_counter() - started_at) * 1000

    logger.info(
        "Tool '%s' | success=%s | latency=%.2f ms",
        tool_name,
        success,
        elapsed_ms,
    )

#endregion


#region NOVACART SUPPORT AGENT

class NovaCartSupportAgent(Agent):

    #region AGENT INITIALIZATION

    def __init__(self) -> None:
        self.state = SessionState(
            call_id=uuid.uuid4().hex[:12]
        )

        knowledge_mcp = build_knowledge_base_mcp()

        runtime_instructions = NOVACART_AGENT_INSTRUCTIONS + """

ADDITIONAL RUNTIME RULES

- When a customer provides a consumer ID, call verify_consumer immediately.
- verify_consumer returns the verified customer's complete order list and complete support-ticket list. Use that information before asking for an order ID or ticket ID.
- Do not ask for an order ID just to list a customer's orders. Ask for or use an order ID only when a specific order must be discussed or acted on.
- If the customer asks for all support tickets, call get_ticket_status without a ticket ID. If they provide a specific ticket ID, retrieve only that ticket.
- For escalation risk, if the customer has not mentioned any previous support contacts, do not guess. Omit previous_contact_count so the tool uses its default value of 0.
- When the conversation reaches a clear final resolution and the customer is ending the interaction, call record_case_outcome exactly once before the final goodbye.
"""

        super().__init__(
            instructions=runtime_instructions,
            tools=[knowledge_mcp],
        )

    #endregion


    #region CUSTOMER VERIFICATION TOOL

    @function_tool
    async def verify_consumer(
        self,
        context: RunContext,
        consumer_id: str,
    ) -> str:
        """
        Verify a customer using their NovaCart consumer ID and return
        all orders and support tickets belonging to that customer.
        """

        started_at = time.perf_counter()
        normalized_consumer_id = consumer_id.strip().upper()
        consumer_connection = open_consumer_database()
        support_connection = None

        try:
            customer = consumer_connection.execute(
                """
                SELECT consumer_id, consumer_name
                FROM consumer_orders
                WHERE consumer_id = ?
                LIMIT 1
                """,
                (normalized_consumer_id,),
            ).fetchone()

            if customer is None:
                log_tool_latency(
                    "verify_consumer",
                    started_at,
                    success=False,
                )
                return (
                    "Consumer verification failed. "
                    "No customer was found with that consumer ID."
                )

            orders = consumer_connection.execute(
                """
                SELECT
                    order_id,
                    order_status,
                    order_date,
                    order_time,
                    ordered_items,
                    order_price,
                    mode_of_payment
                FROM consumer_orders
                WHERE consumer_id = ?
                """,
                (normalized_consumer_id,),
            ).fetchall()

            support_connection = open_support_database()
            tickets = support_connection.execute(
                """
                SELECT
                    ticket_id,
                    order_id,
                    issue_type,
                    issue_summary,
                    priority,
                    status,
                    created_at
                FROM support_tickets
                WHERE consumer_id = ?
                ORDER BY created_at DESC
                """,
                (normalized_consumer_id,),
            ).fetchall()

            self.state.consumer_id = customer["consumer_id"]
            self.state.consumer_name = customer["consumer_name"]
            self.state.verified = True

            # Do not assume a current order when the customer has several.
            self.state.current_order_id = (
                orders[0]["order_id"] if len(orders) == 1 else None
            )

            order_lines = []
            for index, order in enumerate(orders, start=1):
                order_lines.append(
                    f"{index}. Order ID: {order['order_id']}; "
                    f"Status: {order['order_status']}; "
                    f"Date: {order['order_date']} {order['order_time']}; "
                    f"Items: {order['ordered_items']}; "
                    f"Price: ₹{order['order_price']:.2f}; "
                    f"Payment: {order['mode_of_payment']}"
                )

            ticket_lines = []
            for index, ticket in enumerate(tickets, start=1):
                ticket_lines.append(
                    f"{index}. Ticket ID: {ticket['ticket_id']}; "
                    f"Order ID: {ticket['order_id']}; "
                    f"Issue: {ticket['issue_type']}; "
                    f"Priority: {ticket['priority']}; "
                    f"Status: {ticket['status']}"
                )

            orders_text = (
                "\n".join(order_lines)
                if order_lines
                else "No orders found."
            )
            tickets_text = (
                "\n".join(ticket_lines)
                if ticket_lines
                else "No support tickets found."
            )

            log_tool_latency(
                "verify_consumer",
                started_at,
                success=True,
            )

            return (
                f"Consumer verified successfully as {customer['consumer_name']}.\n"
                f"All orders for {normalized_consumer_id}:\n{orders_text}\n"
                f"All support tickets for {normalized_consumer_id}:\n{tickets_text}"
            )

        except Exception:
            logger.exception("Consumer verification failed.")
            log_tool_latency(
                "verify_consumer",
                started_at,
                success=False,
            )
            return "Consumer verification is temporarily unavailable."

        finally:
            consumer_connection.close()
            if support_connection is not None:
                support_connection.close()

    #endregion


    #region ORDER RETRIEVAL TOOL

    @function_tool
    async def get_order_for_consumer(
        self,
        context: RunContext,
        consumer_id: str,
    ) -> str:
        """Retrieve all orders belonging to a verified NovaCart customer."""

        started_at = time.perf_counter()
        normalized_consumer_id = consumer_id.strip().upper()

        if (
            not self.state.verified
            or self.state.consumer_id != normalized_consumer_id
        ):
            return (
                "Customer verification is required before "
                "order information can be retrieved."
            )

        connection = open_consumer_database()

        try:
            orders = connection.execute(
                """
                SELECT
                    order_id,
                    order_status,
                    order_date,
                    order_time,
                    ordered_items,
                    order_price,
                    mode_of_payment
                FROM consumer_orders
                WHERE consumer_id = ?
                """,
                (normalized_consumer_id,),
            ).fetchall()

            if not orders:
                log_tool_latency(
                    "get_order_for_consumer",
                    started_at,
                    success=False,
                )
                return "No orders were found for this customer."

            self.state.current_order_id = (
                orders[0]["order_id"] if len(orders) == 1 else None
            )

            order_lines = []
            for index, order in enumerate(orders, start=1):
                order_lines.append(
                    f"{index}. Order ID: {order['order_id']}; "
                    f"Status: {order['order_status']}; "
                    f"Order date: {order['order_date']}; "
                    f"Order time: {order['order_time']}; "
                    f"Items: {order['ordered_items']}; "
                    f"Order price: ₹{order['order_price']:.2f}; "
                    f"Payment method: {order['mode_of_payment']}"
                )

            log_tool_latency(
                "get_order_for_consumer",
                started_at,
                success=True,
            )

            return (
                f"Found {len(orders)} order(s) for "
                f"{normalized_consumer_id}:\n"
                + "\n".join(order_lines)
            )

        except Exception:
            logger.exception("Order retrieval failed.")
            log_tool_latency(
                "get_order_for_consumer",
                started_at,
                success=False,
            )
            return "Order information is temporarily unavailable."

        finally:
            connection.close()

    #endregion


    #region ORDER LOOKUP BY ID

    @function_tool
    async def get_order_by_id(
        self,
        context: RunContext,
        order_id: str,
    ) -> str:
        """
        Retrieve an order by order ID only if it belongs
        to the currently verified customer.
        """

        started_at = time.perf_counter()
        normalized_order_id = order_id.strip().upper()

        if not self.state.verified or not self.state.consumer_id:
            return (
                "Customer verification is required before "
                "order information can be retrieved."
            )

        connection = open_consumer_database()

        try:
            order = connection.execute(
                """
                SELECT
                    consumer_id,
                    order_id,
                    order_status,
                    order_date,
                    order_time,
                    ordered_items,
                    order_price,
                    mode_of_payment
                FROM consumer_orders
                WHERE order_id = ?
                  AND consumer_id = ?
                """,
                (
                    normalized_order_id,
                    self.state.consumer_id,
                ),
            ).fetchone()

            if order is None:
                log_tool_latency(
                    "get_order_by_id",
                    started_at,
                    success=False,
                )
                return (
                    "No matching order was found for the "
                    "verified customer."
                )

            self.state.current_order_id = order["order_id"]

            log_tool_latency(
                "get_order_by_id",
                started_at,
                success=True,
            )

            return (
                f"Order ID: {order['order_id']}; "
                f"Status: {order['order_status']}; "
                f"Order date: {order['order_date']}; "
                f"Order time: {order['order_time']}; "
                f"Items: {order['ordered_items']}; "
                f"Order price: ₹{order['order_price']:.2f}; "
                f"Payment method: {order['mode_of_payment']}."
            )

        except Exception:
            logger.exception("Order lookup by ID failed.")
            log_tool_latency(
                "get_order_by_id",
                started_at,
                success=False,
            )
            return "Order information is temporarily unavailable."

        finally:
            connection.close()

    #endregion


    #region ESCALATION RISK TOOL

    @function_tool
    async def calculate_escalation_risk(
        self,
        context: RunContext,
        sentiment: str,
        issue_unresolved: bool,
        order_status: str,
        order_price: float,
        previous_contact_count: int = 0,
    ) -> str:
        """
        Estimate escalation risk using sentiment, repeated contact,
        unresolved issues, order status, and order value.

        If previous contact history is not mentioned, the default count is 0.
        """

        started_at = time.perf_counter()

        valid_sentiments = {
            "positive",
            "neutral",
            "negative",
            "very_negative",
        }

        normalized_sentiment = sentiment.strip().lower()
        normalized_status = order_status.strip().lower()
        previous_contact_count = max(previous_contact_count, 0)

        if normalized_sentiment not in valid_sentiments:
            log_tool_latency(
                "calculate_escalation_risk",
                started_at,
                success=False,
            )
            return (
                "Invalid sentiment value. Use positive, neutral, "
                "negative, or very_negative."
            )

        score = 0

        if normalized_sentiment == "negative":
            score += 2
        elif normalized_sentiment == "very_negative":
            score += 4

        if previous_contact_count >= 3:
            score += 3
        elif previous_contact_count >= 1:
            score += 1

        if issue_unresolved:
            score += 2

        if normalized_status == "delayed":
            score += 1

        if order_price >= 15000:
            score += 1

        if score >= 7:
            risk = "HIGH"
        elif score >= 4:
            risk = "MEDIUM"
        else:
            risk = "LOW"

        self.state.escalation_risk = risk

        log_tool_latency(
            "calculate_escalation_risk",
            started_at,
            success=True,
        )

        return (
            f"Escalation risk: {risk}. "
            f"Previous contact count used: {previous_contact_count}."
        )

    #endregion


    #region SUPPORT TICKET CREATION TOOL

    @function_tool
    async def create_support_ticket(
        self,
        context: RunContext,
        consumer_id: str,
        order_id: str,
        issue_type: str,
        issue_summary: str,
        priority: str,
    ) -> str:
        """Create a support ticket for the verified customer."""

        started_at = time.perf_counter()

        normalized_consumer_id = consumer_id.strip().upper()
        normalized_order_id = order_id.strip().upper()
        normalized_priority = priority.strip().lower()
        normalized_issue_type = issue_type.strip().lower()

        valid_priorities = {
            "low",
            "medium",
            "high",
            "urgent",
        }

        if (
            not self.state.verified
            or self.state.consumer_id != normalized_consumer_id
        ):
            log_tool_latency(
                "create_support_ticket",
                started_at,
                success=False,
            )
            return (
                "Customer verification is required before "
                "a support ticket can be created."
            )

        if normalized_priority not in valid_priorities:
            log_tool_latency(
                "create_support_ticket",
                started_at,
                success=False,
            )
            return "Invalid ticket priority."

        if len(issue_summary.strip()) < 5:
            log_tool_latency(
                "create_support_ticket",
                started_at,
                success=False,
            )
            return "Issue summary is too short."

        consumer_connection = open_consumer_database()

        try:
            order = consumer_connection.execute(
                """
                SELECT order_id
                FROM consumer_orders
                WHERE consumer_id = ?
                  AND order_id = ?
                """,
                (
                    normalized_consumer_id,
                    normalized_order_id,
                ),
            ).fetchone()

            if order is None:
                log_tool_latency(
                    "create_support_ticket",
                    started_at,
                    success=False,
                )
                return (
                    "The supplied order does not belong to "
                    "the verified customer."
                )

        finally:
            consumer_connection.close()

        ticket_id = "T" + uuid.uuid4().hex[:8].upper()
        created_at = datetime.now(timezone.utc).isoformat()

        support_connection = open_support_database()

        try:
            support_connection.execute(
                """
                INSERT INTO support_tickets (
                    ticket_id,
                    consumer_id,
                    order_id,
                    issue_type,
                    issue_summary,
                    priority,
                    status,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ticket_id,
                    normalized_consumer_id,
                    normalized_order_id,
                    normalized_issue_type,
                    issue_summary.strip()[:500],
                    normalized_priority,
                    "Open",
                    created_at,
                ),
            )

            support_connection.commit()
            self.state.created_ticket_id = ticket_id

            log_tool_latency(
                "create_support_ticket",
                started_at,
                success=True,
            )

            return (
                f"Support ticket {ticket_id} was created successfully "
                f"with {normalized_priority} priority."
            )

        except Exception:
            logger.exception("Support ticket creation failed.")
            log_tool_latency(
                "create_support_ticket",
                started_at,
                success=False,
            )
            return "Support ticket creation is temporarily unavailable."

        finally:
            support_connection.close()

    #endregion


    #region SUPPORT TICKET STATUS TOOL

    @function_tool
    async def get_ticket_status(
        self,
        context: RunContext,
        ticket_id: str | None = None,
    ) -> str:
        """
        Retrieve one support ticket by ID, or all support tickets when
        no ticket ID is supplied, for the currently verified customer.
        """

        started_at = time.perf_counter()

        if not self.state.verified or not self.state.consumer_id:
            log_tool_latency(
                "get_ticket_status",
                started_at,
                success=False,
            )
            return (
                "Customer verification is required before "
                "ticket information can be retrieved."
            )

        normalized_ticket_id = (
            ticket_id.strip().upper()
            if ticket_id and ticket_id.strip()
            else None
        )

        connection = open_support_database()

        try:
            if normalized_ticket_id:
                ticket = connection.execute(
                    """
                    SELECT
                        ticket_id,
                        order_id,
                        issue_type,
                        issue_summary,
                        priority,
                        status,
                        created_at
                    FROM support_tickets
                    WHERE ticket_id = ?
                      AND consumer_id = ?
                    """,
                    (
                        normalized_ticket_id,
                        self.state.consumer_id,
                    ),
                ).fetchone()

                if ticket is None:
                    log_tool_latency(
                        "get_ticket_status",
                        started_at,
                        success=False,
                    )
                    return (
                        "No matching support ticket was found "
                        "for the verified customer."
                    )

                log_tool_latency(
                    "get_ticket_status",
                    started_at,
                    success=True,
                )

                return (
                    f"Ticket ID: {ticket['ticket_id']}; "
                    f"Status: {ticket['status']}; "
                    f"Priority: {ticket['priority']}; "
                    f"Issue type: {ticket['issue_type']}; "
                    f"Issue summary: {ticket['issue_summary']}; "
                    f"Order ID: {ticket['order_id']}; "
                    f"Created at: {ticket['created_at']}."
                )

            tickets = connection.execute(
                """
                SELECT
                    ticket_id,
                    order_id,
                    issue_type,
                    issue_summary,
                    priority,
                    status,
                    created_at
                FROM support_tickets
                WHERE consumer_id = ?
                ORDER BY created_at DESC
                """,
                (self.state.consumer_id,),
            ).fetchall()

            if not tickets:
                log_tool_latency(
                    "get_ticket_status",
                    started_at,
                    success=True,
                )
                return "No support tickets were found for this customer."

            ticket_lines = []
            for index, ticket in enumerate(tickets, start=1):
                ticket_lines.append(
                    f"{index}. Ticket ID: {ticket['ticket_id']}; "
                    f"Status: {ticket['status']}; "
                    f"Priority: {ticket['priority']}; "
                    f"Issue type: {ticket['issue_type']}; "
                    f"Order ID: {ticket['order_id']}; "
                    f"Created at: {ticket['created_at']}"
                )

            log_tool_latency(
                "get_ticket_status",
                started_at,
                success=True,
            )

            return (
                f"Found {len(tickets)} support ticket(s) for "
                f"{self.state.consumer_id}:\n"
                + "\n".join(ticket_lines)
            )

        except Exception:
            logger.exception("Ticket status lookup failed.")
            log_tool_latency(
                "get_ticket_status",
                started_at,
                success=False,
            )
            return "Ticket information is temporarily unavailable."

        finally:
            connection.close()

    #endregion


    #region CALL OUTCOME TOOL

    @function_tool
    async def record_case_outcome(
        self,
        context: RunContext,
        intent: str,
        sentiment: str,
        resolution: str,
    ) -> str:
        """Record the final outcome of the current support conversation."""

        started_at = time.perf_counter()

        if self.state.outcome_recorded:
            log_tool_latency(
                "record_case_outcome",
                started_at,
                success=False,
            )
            return "Call outcome has already been recorded."

        connection = open_support_database()

        try:
            connection.execute(
                """
                INSERT INTO call_outcomes (
                    call_id,
                    consumer_id,
                    order_id,
                    intent,
                    sentiment,
                    escalation_risk,
                    ticket_id,
                    resolution,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.state.call_id,
                    self.state.consumer_id,
                    self.state.current_order_id,
                    intent.strip().lower(),
                    sentiment.strip().lower(),
                    self.state.escalation_risk,
                    self.state.created_ticket_id,
                    resolution.strip()[:500],
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

            connection.commit()
            self.state.outcome_recorded = True

            log_tool_latency(
                "record_case_outcome",
                started_at,
                success=True,
            )

            return "Call outcome recorded successfully."

        except Exception:
            logger.exception("Call outcome recording failed.")
            log_tool_latency(
                "record_case_outcome",
                started_at,
                success=False,
            )
            return "Call outcome could not be recorded."

        finally:
            connection.close()

    #endregion


    #region AGENT GREETING

    async def on_enter(self) -> None:
        """Greet the customer when the voice session begins."""

        await self.session.say(
            "Hi! Thanks for contacting NovaCart support. "
            "How can I help you with your order today?"
        )

    #endregion

#endregion


#region AI MODEL CONFIGURATION

def create_ai_models():
    """
    Configure the speech recognition, language model,
    and text-to-speech models used by the voice agent.
    """

    stt_model = inference.STT(
        model="deepgram/nova-3",
        language="en",
        fallback=[
            {"model": "assemblyai/universal-streaming"}
        ],
    )

    llm_model = llm.FallbackAdapter(
        [
            inference.LLM(model="google/gemma-4-31b-it"),
            inference.LLM(model="google/gemini-3.5-flash"),
        ]
    )

    tts_model = inference.TTS(
        model="cartesia/sonic-3",
        voice="f31cc6a7-c1e8-4764-980c-60a361443dd1",
        language="en",
        extra_kwargs={
            "speed": 1.0,
        },
        fallback=[
            {
                "model": "inworld/inworld-tts-2",
                "voice": "Ashley",
                "extra_kwargs": {
                    "speaking_rate": 1.0,
                },
            }
        ],
    )

    return stt_model, llm_model, tts_model

#endregion


#region LIVEKIT VOICE SESSION

def create_voice_session() -> AgentSession:
    """Create the real-time LiveKit voice pipeline."""

    stt_model, llm_model, tts_model = create_ai_models()

    session = AgentSession(
        stt=stt_model,
        llm=llm_model,
        tts=tts_model,
        vad=ai_coustics.VAD(),
        turn_handling=TurnHandlingOptions(
            turn_detection=inference.TurnDetector(),
            endpointing={
                "mode": "fixed",
                "min_delay": 0.4,
                "max_delay": 2.5,
            },
            interruption={
                "mode": "adaptive",
                "min_duration": 0.5,
                "min_words": 0,
                "false_interruption_timeout": 2.0,
                "resume_false_interruption": True,
            },
            preemptive_generation={
                "enabled": True,
                "preemptive_tts": False,
            },
        ),
    )

    return session

#endregion


#region LIVEKIT SERVER

server = AgentServer()


@server.rtc_session(agent_name="customer-support-620")
async def entrypoint(ctx: JobContext) -> None:
    """Start one NovaCart voice-support session for a LiveKit room."""

    validate_consumer_database()
    validate_knowledge_base()
    initialize_support_database()

    session = create_voice_session()

    await session.start(
        agent=NovaCartSupportAgent(),
        room=ctx.room,
        room_options=room_io.RoomOptions(
            audio_input=room_io.AudioInputOptions(
                noise_cancellation=ai_coustics.audio_enhancement(
                    model=ai_coustics.EnhancerModel.QUAIL_VF_S
                )
            )
        ),
    )

#endregion


#region RUN APPLICATION

if __name__ == "__main__":
    cli.run_app(server)

#endregion
