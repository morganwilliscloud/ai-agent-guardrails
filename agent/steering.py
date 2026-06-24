import logging
import os
from typing import Any

from strands import Agent
from strands.experimental.steering import SteeringHandler, Proceed, Guide
from strands.models import BedrockModel
from strands.types.content import Message
from strands.types.streaming import StopReason

logger = logging.getLogger(__name__)

REVIEW_POLICY = """You review customer service responses for policy violations.

REJECT if the response:
1. UNCONFIRMED PROMISES: Claims an action was completed (refund processed, case created,
   label generated) but NO tool was called at all in the conversation to perform it.
   If a tool WAS called and returned a successful result, the agent MAY summarize or
   paraphrase the outcome — that is NOT a violation. Only reject if there is zero
   tool evidence for the claim.
   NOTE: A successful return label generation is indicated by the tool returning a URL
   like https://returns.anycompany-robotics.com/label/RET-XXXXXX. If the agent includes
   a return label URL, it MUST exactly match the URL from the tool result.
2. INTERNAL SYSTEM DETAILS: Mentions DynamoDB, Lambda, S3, Cedar, policy engine,
   gateway, interceptor, AgentCore, Bedrock, tool names like "issue-refund-tool",
   or any AWS service name. The customer should never see infrastructure details.
3. OVER-PROMISING ON ESCALATIONS: Says "our team WILL process your refund" or
   "you WILL receive a full refund" when the case was only escalated for review.
   Escalations should say "will review" not "will process."
4. HALLUCINATED CONTACT INFO: Includes phone numbers, email addresses, or URLs
   that were not returned by any tool in the conversation.

APPROVE if the response:
- Only states facts confirmed by tool results (summarizing is fine)
- Uses professional, empathetic tone
- Appropriately hedges on escalated cases ("will review" not "will process")
- Does not expose any internal system details

RESPONSE FORMAT:
APPROVE
or
REJECT: [quote the exact violation and name which rule it breaks]"""


class CustomerServiceSteeringHandler(SteeringHandler):
    """Evaluates agent responses against customer service policies."""

    def __init__(self, max_retries: int = 2):
        super().__init__(context_providers=[])
        self.max_retries = max_retries
        self.retry_count = 0
        self.reviewer_model = BedrockModel(
            model_id=os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-20250514-v1:0"),
            region_name=os.environ.get("AWS_REGION", "us-east-1"),
        )

    async def steer_after_model(self, *, agent: "Agent", message: Message, stop_reason: StopReason, **kwargs: Any):
        # Extract response text
        text = ""
        for block in message.get("content", []):
            if "text" in block:
                text += block["text"]

        if not text or len(text) < 50:
            return Proceed(reason="Response too short to evaluate")

        # Only evaluate final responses, not intermediate tool-calling messages
        if stop_reason == "tool_use":
            return Proceed(reason="Agent is calling tools, skipping evaluation")

        # If the response contains internal system references from guardrails,
        # clean it up directly without asking the model (which would get blocked again)
        lower = text.lower()
        if "guardrail" in lower or "bedrock" in lower or "blocked by" in lower:
            logger.warning("[STEERING] Internal system reference detected, cleaning up")
            for block in message.get("content", []):
                if "text" in block:
                    block["text"] = ("I can only help with AnyCompany Robotics robot vacuum "
                                     "products and services. How can I assist you today?")
            return Proceed(reason="Cleaned up internal system reference")

        if self.retry_count >= self.max_retries:
            self.retry_count = 0
            return Proceed(reason="Max retries reached, accepting output")

        # Gather tool results from conversation history so the reviewer has context
        tool_results_summary = self._extract_tool_results(agent)

        # Evaluate with the reviewer
        reviewer = Agent(
            model=self.reviewer_model,
            system_prompt=REVIEW_POLICY,
            callback_handler=None,
        )
        review_input = f"TOOL RESULTS FROM THIS CONVERSATION:\n{tool_results_summary}\n\nRESPONSE TO EVALUATE:\n{text}"
        result = str(reviewer(review_input))
        logger.info("[STEERING] %s", result[:200])

        if "REJECT:" in result.upper():
            self.retry_count += 1
            feedback = result.split("REJECT:", 1)[-1].strip()
            logger.warning("[STEERING] Guiding rewrite (attempt %d/%d): %s",
                          self.retry_count, self.max_retries, feedback[:200])
            return Guide(reason=f"Fix this issue: {feedback[:300]}. "
                                "Only fix the cited issue. Do not add apologies or meta-commentary. "
                                "Output only the customer-facing response.")

        self.retry_count = 0
        return Proceed(reason="Response approved by steering reviewer")

    def _extract_tool_results(self, agent) -> str:
        """Extract tool results from the agent's conversation messages."""
        results = []
        try:
            messages = getattr(agent, "messages", []) or []
            for msg in messages:
                for block in msg.get("content", []):
                    if "toolResult" in block:
                        tool_name = block.get("toolResult", {}).get("toolUseId", "unknown")
                        content = block["toolResult"].get("content", [])
                        text_parts = []
                        for part in content:
                            if "text" in part:
                                text_parts.append(part["text"])
                            elif "json" in part:
                                text_parts.append(str(part["json"]))
                        if text_parts:
                            results.append(f"Tool result: {' '.join(text_parts)[:500]}")
        except Exception as e:
            logger.warning("[STEERING] Could not extract tool results: %s", e)
        return "\n".join(results) if results else "(no tool results found)"
