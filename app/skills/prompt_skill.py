"""Prompt Skill - Intelligent input validation and analytical intent inspection.

Validates incoming user prompts before they reach the AI model:
1. Detects gibberish, key-mashing, and random symbol noise.
2. Identifies conversational greetings and conversational filler without analytical intent.
3. Screens off-topic domain queries (recipes, sports, jokes, general knowledge, coding tasks).
4. Catches overly vague or uninformative single-word prompts.
5. Accurately approves operational analytical queries in both English and Hinglish.
6. Returns clear, user-friendly explanatory messages and actionable prompt suggestions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any


@dataclass
class PromptValidationResult:
    is_valid: bool
    category: str  # "valid", "gibberish", "greeting_only", "off_topic", "too_vague"
    message: str
    reason: str
    confidence: float
    suggested_prompts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_valid": self.is_valid,
            "category": self.category,
            "message": self.message,
            "reason": self.reason,
            "confidence": self.confidence,
            "suggested_prompts": self.suggested_prompts,
        }


class PromptSkill:
    """Server-side prompt inspection and analytical validation skill."""

    # Domain entities (logistics, shipment, remittance, finances, geography)
    DOMAIN_ENTITIES: set[str] = {
        "shipment", "shipments", "order", "orders", "courier", "couriers",
        "carrier", "carriers", "delivery", "delivered", "transit", "undelivered",
        "rto", "ndr", "cancellation", "cancellations", "charge", "charges",
        "freight", "weight", "city", "cities", "state", "states", "destination",
        "destinations", "payout", "payouts", "remittance", "remittances",
        "cod", "prepaid", "revenue", "cost", "costs", "bottleneck", "bottlenecks",
        "delay", "delays", "tat", "awb", "failed", "failure", "failures",
        "bluedart", "delhivery", "shadowfax", "xpressbees", "ekart", "dtdc",
        "zone", "zones", "pincode", "pincodes", "exception", "exceptions",
        "hold", "aging", "utr", "clearing", "payout", "discrepancy", "discrepancies",
    }

    # Analytical action verbs and intent indicators
    ANALYTICAL_ACTIONS: set[str] = {
        "analyze", "analyse", "analysis", "compare", "comparison", "show",
        "generate", "report", "breakdown", "summary", "summarize", "summarise",
        "chart", "charts", "graph", "graphs", "plot", "plots", "bar", "pie",
        "donut", "doughnut", "area", "line", "trend", "trends", "kpi", "kpis",
        "metric", "metrics", "evaluate", "evaluation", "inspect", "detail",
        "detailed", "table", "tables", "tabular", "top", "bottom", "highest",
        "lowest", "best", "worst", "reasons", "audit", "review", "highlight",
        "track", "check", "measure", "dashboard", "overview", "performance",
    }

    # Hinglish intent keywords commonly used by Indian logistics operators
    HINGLISH_KEYWORDS: set[str] = {
        "chahiye", "batao", "dikhao", "karo", "kya", "hai", "kitna", "konsa",
        "sabse", "jyada", "kam", "wali", "wale", "kaisa", "kaise", "hoga",
        "dekho", "nikalo", "mujhe", "meri", "mera", "hamaara", "hamara",
    }

    # Pure conversational greetings and filler
    GREETINGS: set[str] = {
        "hi", "hello", "hey", "hola", "namaste", "test", "testing", "ok",
        "okay", "k", "cool", "thanks", "thank", "you", "bye", "gm", "gn", "sup",
        "yo", "morning", "evening", "there", "team", "bot", "ai",
    }

    # Off-topic domain indicators (words that clearly belong to unrelated domains)
    OFF_TOPIC_KEYWORDS: set[str] = {
        "recipe", "recipes", "cook", "cooking", "bake", "baking", "pizza", "burger", "biryani",
        "cake", "chocolate", "food", "restaurant",
        "cricket", "football", "soccer", "ipl", "score", "match", "tournament",
        "movie", "film", "cinema", "actor", "actress", "song", "lyrics", "poem", "poetry",
        "joke", "jokes", "funny", "laugh", "meme",
        "weather", "rain", "temperature",
        "girlfriend", "boyfriend", "dating", "love",
        "president", "election", "politics", "politician", "minister",
        "crypto", "bitcoin", "ethereum",
        "python", "java", "javascript", "code", "coding", "binary", "algorithm",
        "homework", "essay", "horoscope", "astrology", "game", "gaming", "playstation",
    }

    DEFAULT_SUGGESTIONS: dict[str, list[str]] = {
        "shipment": [
            "Analyse my shipment data and show courier delivery performance and RTO rates.",
            "Include a bar chart of courier volume and a donut chart of delivery status breakdown.",
            "Show delivery failure reasons, city-wise delivery breakdown, and operational bottlenecks.",
            "Compare prepaid vs COD delivery success rates across top cities.",
        ],
        "paid_remittance": [
            "Analyze cleared COD remittances, deduction breakdown, and payout batches.",
            "Show total remitted amount and transaction status distribution.",
            "Compare remittance charges and early COD payout adoption share.",
        ],
        "pending_remittance": [
            "Analyze pending COD payouts, hold statuses, and projected deductions.",
            "Show upcoming remittance schedule and overdue balances.",
            "Identify hold reasons and recommended steps to expedite payout clearance.",
        ],
    }

    def __init__(self) -> None:
        pass

    def get_skill_blueprint(self) -> dict[str, Any]:
        """Return the skill blueprint and operational guidelines."""
        return {
            "name": "prompt-validation-skill",
            "version": "1.0.0",
            "description": (
                "Server-side prompt inspection skill ensuring incoming requests contain "
                "valid analytical intent for logistics and financial reports before model execution."
            ),
            "capabilities": [
                "Zero-latency heuristic gibberish & noise detection",
                "Conversational greeting filtering",
                "Off-topic domain screening",
                "Vague request detection",
                "Full English and Hinglish analytical intent recognition",
                "Actionable suggestions tailored to report type",
            ],
            "categories": [
                "valid", "gibberish", "greeting_only", "off_topic", "too_vague"
            ],
        }

    def get_suggestions(self, report_type: str = "shipment") -> list[str]:
        """Return curated prompt suggestions for a given report type."""
        norm = str(report_type).strip().lower().replace("-", "_")
        if "pending" in norm:
            return list(self.DEFAULT_SUGGESTIONS["pending_remittance"])
        if "remittance" in norm:
            return list(self.DEFAULT_SUGGESTIONS["paid_remittance"])
        return list(self.DEFAULT_SUGGESTIONS["shipment"])

    def validate(self, prompt: str, report_type: str = "shipment") -> PromptValidationResult:
        """Validate whether the user prompt represents a meaningful analytical instruction."""
        suggestions = self.get_suggestions(report_type)
        raw = (prompt or "").strip()

        # 1. Empty or whitespace check
        if not raw:
            return PromptValidationResult(
                is_valid=False,
                category="too_vague",
                message="Please enter what you would like to analyse in your report.",
                reason="empty_prompt",
                confidence=1.0,
                suggested_prompts=suggestions,
            )

        text_lower = raw.lower()
        words = re.findall(r"\b[a-z0-9_'-]+\b", text_lower)

        # 2. Minimum length and word count check
        if len(raw) < 2 or not words:
            return PromptValidationResult(
                is_valid=False,
                category="gibberish",
                message="Your prompt is too short. Please enter a specific analysis request (e.g. 'Analyze courier delivery performance and RTO rates').",
                reason="insufficient_length",
                confidence=1.0,
                suggested_prompts=suggestions,
            )

        # 3. Gibberish & Key-mash detection
        is_gibberish, gibberish_reason = self._check_gibberish(raw, words)
        if is_gibberish:
            return PromptValidationResult(
                is_valid=False,
                category="gibberish",
                message="Your prompt could not be understood. Please enter a meaningful request describing what to analyse in your shipment data.",
                reason=gibberish_reason,
                confidence=0.95,
                suggested_prompts=suggestions,
            )

        # 4. Greeting / Conversational filler only
        if self._is_greeting_only(words, text_lower):
            return PromptValidationResult(
                is_valid=False,
                category="greeting_only",
                message="Your prompt appears to be a greeting. Please specify what you would like to analyze in your shipment or remittance data.",
                reason="conversational_greeting_without_analytical_instruction",
                confidence=0.95,
                suggested_prompts=suggestions,
            )

        # 5. Domain & Analytical Token Audit
        domain_matches = [w for w in words if w in self.DOMAIN_ENTITIES]
        action_matches = [w for w in words if w in self.ANALYTICAL_ACTIONS]
        hinglish_matches = [w for w in words if w in self.HINGLISH_KEYWORDS]
        off_topic_matches = [w for w in words if w in self.OFF_TOPIC_KEYWORDS]

        # 6. Off-Topic Screening (e.g. recipes, cinema, sports, coding)
        if off_topic_matches and not domain_matches and not action_matches:
            matched_topics = ", ".join(off_topic_matches[:2])
            return PromptValidationResult(
                is_valid=False,
                category="off_topic",
                message=(
                    f"This assistant specializes in logistics, shipment, and remittance analytics. "
                    f"Your prompt appears unrelated to business data analysis. Please describe what you want to analyze."
                ),
                reason=f"off_topic_keywords_detected: {matched_topics}",
                confidence=0.90,
                suggested_prompts=suggestions,
            )

        # 7. Too Vague / Generic Single Words
        # e.g. "analyze", "data", "report", "something", "do it"
        if len(words) <= 2:
            if not domain_matches and not (action_matches and len(words) >= 2):
                if words[0] in {"data", "something", "anything", "please", "help", "view", "check"}:
                    return PromptValidationResult(
                        is_valid=False,
                        category="too_vague",
                        message="Your prompt is too vague. Please tell us which specific metrics or couriers you want to examine.",
                        reason="vague_short_query",
                        confidence=0.88,
                        suggested_prompts=suggestions,
                    )

        # 8. Success: Has domain context, analytical instruction, or Hinglish query
        has_analytics = bool(action_matches or domain_matches or hinglish_matches)

        # Even if no strict keyword matched, if the prompt is structured (e.g. asking for specific charts or comparisons)
        chart_spec_pattern = bool(re.search(r"\b(chart|graph|donut|bar|pie|table|area|line|column)\b", text_lower))
        comparison_pattern = bool(re.search(r"\b(compare|vs|versus|breakdown|reasons?|rate|trend)\b", text_lower))

        if has_analytics or chart_spec_pattern or comparison_pattern:
            return PromptValidationResult(
                is_valid=True,
                category="valid",
                message="Valid analytical prompt.",
                reason="operational_analytical_intent_detected",
                confidence=0.98,
                suggested_prompts=suggestions,
            )

        # If sentence is 4+ words of natural language without off-topic flags, assume reasonable user intent
        if len(words) >= 4 and not off_topic_matches:
            return PromptValidationResult(
                is_valid=True,
                category="valid",
                message="Valid natural language query.",
                reason="natural_language_context",
                confidence=0.75,
                suggested_prompts=suggestions,
            )

        # Borderline vague input
        return PromptValidationResult(
            is_valid=False,
            category="too_vague",
            message="We couldn't identify what you want to analyze. Please specify courier, delivery, RTO, or financial metrics to include in your report.",
            reason="unrecognized_analytical_intent",
            confidence=0.80,
            suggested_prompts=suggestions,
        )

    def _check_gibberish(self, raw: str, words: list[str]) -> tuple[bool, str]:
        """Check for keyboard mashing, repeated characters, and symbol spam."""
        # Consecutive identical characters (e.g. "aaaaaa", "111111", "zzzzzz")
        if re.search(r"(.)\1{4,}", raw):
            return True, "consecutive_repeated_characters"

        # Symbol spam / non-alphanumeric ratio
        alphanumeric_count = sum(c.isalnum() or c.isspace() for c in raw)
        if len(raw) >= 6 and (alphanumeric_count / len(raw)) < 0.35:
            return True, "excessive_symbols_or_punctuation"

        # Check individual words for consonant clustering without vowels
        vowels = set("aeiouy")
        all_numeric = all(w.isdigit() for w in words)
        if all_numeric and len(words) >= 1 and len(raw) > 5:
            return True, "pure_numeric_noise"

        for word in words:
            # Skip numbers or short words
            if word.isdigit() or len(word) < 5:
                continue

            # Long consonant run (e.g. "asdfghjk", "qwertz", "xcvbnm", "hjkfghs")
            consonant_run = re.search(r"[bcdfghjklmnpqrstvwxz]{6,}", word)
            if consonant_run:
                return True, f"unnatural_consonant_clustering: {word}"

            # Vowel ratio in long alphabetic words
            alpha_only = [c for c in word if c.isalpha()]
            if len(alpha_only) >= 7:
                vowel_count = sum(c in vowels for c in alpha_only)
                if vowel_count == 0 or (vowel_count / len(alpha_only)) < 0.12:
                    return True, f"unnatural_vowel_ratio: {word}"

        return False, ""

    def _is_greeting_only(self, words: list[str], text_lower: str) -> bool:
        """Check if the prompt is exclusively a greeting or conversational filler."""
        if not words:
            return False

        # Single or double word greeting
        if len(words) <= 3 and all(w in self.GREETINGS for w in words):
            return True

        # Common multi-word greetings
        for phrase in ("good morning", "good afternoon", "good evening", "good night", "thank you"):
            if text_lower.strip() == phrase:
                return True

        return False


prompt_skill = PromptSkill()
