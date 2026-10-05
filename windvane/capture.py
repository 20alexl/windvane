"""
Decision capture: one judgement behind every stored decision.

``capture_decision(text)`` is what the prompt hook (a typed prompt, live)
and the session miner (a correction found in a transcript) both call, so a
sentence is stored or not by the same rule wherever it was seen. Three
parts, all here:

1. The semantic tier (``score_decision_semantic``): cosine similarity of
   the sentence against decision and non-decision templates, computed by
   the windvane daemon with the configured sentence-transformers encoder.
   A hook process never loads the model itself; with no daemon answering,
   this tier scores 0 and the regex tier decides.
2. The regex tier (``_score_decision_intent``): weighted keywords and
   sentence structure, with a small typo corrector for the trigger words.
   Always available, stdlib only.
3. The shape gate (``looks_like_decision`` / ``looks_like_correction``): a
   decision is a declarative sentence of at least four words, not a
   question, not a count or a table row or a commit report, not an
   acknowledgement, and it carries a word that decides something.

The best tier score has to reach ``CAPTURE_THRESHOLD`` and the winner has
to pass the gate. The template embeddings are cached under the store
(``<store>/embeddings/decision_templates.json``), stamped with the encoder's
signature so a model change rebuilds them.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Optional

# ===========================================================================
# The shape gate
# ===========================================================================
#
# Both capture paths (the miner over past transcripts, the prompt hook over
# the live one) used to store anything their scorers let through:
# questions, acknowledgements, test counts, pasted status lines, sentence
# fragments. Eight days of one session put 636 such entries into one store,
# and they came back before edits as "Relevant memories for this file".
# This gate is about FORM, never about any particular user's phrasing.

_PREFIX = re.compile(r"^\s*(?:(?:DECISION:|USER PREFERENCE:|\(from user\)|\(confirmed\))\s*)+", re.IGNORECASE)
# Acknowledgement words, alone or strung together ("ok looks good, thanks").
_ACK = re.compile(
    r"^(?:(?:ok(?:ay)?|yes|yeah|yep|sure|fine|good|great|nice|approved?|confirmed?|go ahead|do it|"
    r"sounds good|looks good|proceed|thanks|thank you|perfect|correct|right|agreed|noted|please|cool|done)"
    r"\b[\s,.!;:-]*)+$",
    re.IGNORECASE,
)
# A word that decides: a choice, a rule, a direction, a permission. Tuned
# on the neutral 260-prompt corpus in tests/test_capture.py (120 decisions,
# the rest not).
_CUE = re.compile(
    r"\b(?:let'?s|we'?ll|i'?ll|we should|should(?:n'?t)?|always|never|from now on|going forward|"
    r"instead|switch(?:ed|ing)? to|adopt|keep|drop|go(?:ing)? with|went with|decided?|decision|"
    r"approved?|prefer(?:red)?|stick with|rule|policy|default|use|do not|don'?t|stop|leave|"
    r"only|must|pause|no longer|rather than|allowed|forbidden|required|optional|implement|"
    r"lock in|pick|choose|chose|commit to|settle on|standardi[sz]e on|needs? to|needs? at least|"
    r"require[sd]?|go native|move to|not .{1,30}\b(?:but|instead)\b)\b",
    re.IGNORECASE,
)
# A "(confirmed)" entry is an assistant sentence the user said yes to. It
# is a decision only when that sentence PROPOSES something (first person,
# a plan, a recommendation); an explanation the user agreed with is not.
# Three hours of mining stored 45 such entries, contrast phrases ("X, not
# Y") in ordinary prose among them (2026-09-23).
_PROPOSAL = re.compile(
    r"^(?:\W*\w+\W+){0,3}(?:i'?ll|i will|i'?d|let'?s|we'?ll|we should|we could|i propose|i recommend|i suggest|"
    r"my recommendation|the plan is|plan:|proposal:|going to|next i|next,? i|i want to|i'?m going to)\b",
    re.IGNORECASE,
)
# An edit verb is an instruction on its own ("rename this variable", "revert
# the last commit") and a decision when it names a transition ("replace X
# with Y", "migrate from A to B") or a scope ("rename every handler") and
# its object is not deictic ("this file", "that variable").
_ACTION_CUE = re.compile(
    r"\b(?:rename|replace|move|split|merge|revert|remove|undo|delete|rewrite|migrate|swap|convert|upgrade|switch)\b",
    re.IGNORECASE,
)
_ACTION_DEICTIC = re.compile(
    r"\b(?:rename|replace|move|split|merge|revert|remove|undo|delete|rewrite|migrate|swap|convert|upgrade|switch)\b"
    r"(?:\s+\S+){0,2}\s+(?:this|that|these|those|the last|the latest)\b",
    re.IGNORECASE,
)
_SCOPE = re.compile(r"\b(?:all|every|always|never|any|new|from now on|going forward|whole|everywhere|across|each|no longer)\b", re.IGNORECASE)
_TRANSITION = re.compile(r"\bfrom\b.*\bto\b|\bwith\b|\bover\b|\busing\b|\bto\b", re.IGNORECASE)
# Hedges, history, opinion and third parties: talk about a choice, not a
# choice of ours.
_HEDGE = re.compile(
    r"\b(?:not sure|unsure|not certain|no idea|wondering|whether|maybe|perhaps|might|could potentially|"
    r"potentially|thinking about|think about|consider(?:ing)?|used to|were going to|was going to|personally|"
    r"(?:most|many|some|other|several) (?:people|teams|projects|folks|companies|devs|developers)|"
    r"our competitors|the previous team|the old team|use case|"
    # a leaning held loosely: a guess, a choice left open, a choice handed back (2026-09-25)
    r"probably|possibly|i guess|i'?d guess|i suppose|leaning toward|open to it|up to you|either works|"
    r"no strong opinion|if you think so|or not\W*$)",
    re.IGNORECASE,
)
# A question typed without its mark: an auxiliary or a question word opens
# the sentence ("would it be better to split", "any reason not to use the
# queue"). "Do not", "should never" and "should always" open rules, not
# questions (2026-09-25).
_QUESTION_OPENER = re.compile(
    r"^(?:(?:so|and|but|or|ok(?:ay)?)[\s,]+)?"
    r"(?:(?:would|should|could|can|is|are|do|does|did|will|what|which|how|why)\b"
    r"(?!\s+(?:not|never|always)\b)|any reason\b)",
    re.IGNORECASE,
)
# An approval verdict: a plan under discussion was approved. The plan is
# not in the sentence, so the entry would say only that something was
# approved; a rule with an approval in front of it ("approved: from now on
# always ...") keeps its other deciding word and stays (2026-09-25).
_APPROVAL_WORDS = re.compile(
    r"\b(?:approved?|approval|accepted|green[- ]?light(?:ed)?|sign(?:ed)?[- ]?off|proceed|go ahead|"
    r"as written|no changes|lgtm)\b",
    re.IGNORECASE,
)
# A word that redirects: what a correction or a preference carries. Tuned
# on the neutral corpus in tests/test_capture.py (positives: its negation
# and convention decisions; negatives: every not-decision), never on any
# one session: the broad first list (with "other", "should", "want",
# "actually", "mean") scored precision 0.74, recall 0.57; this one 0.86 /
# 0.80. Position did not matter (the same list within the first six words:
# 0.86 / 0.78). "undo" and "revert" are commands more often than
# corrections and are left to the decision cue.
_CORRECTION_CUE = re.compile(
    r"\b(?:no|not|don'?t|doesn'?t|isn'?t|wasn'?t|never|wrong|instead|stop|rather than|shouldn'?t|"
    r"meant|prefer|differently|always|avoid|only)\b",
    re.IGNORECASE,
)
# Starts like code, markup, a path, a URL, a quote, a list marker or a number.
_CODE_START = re.compile(r"^\s*(?:[`\"'|#>$<-]|\w:[\\/]|/[a-z]|\.\.?/|https?://|\d)")
# Markup or machine text anywhere: a tag, a JSON edge, an escaped quote.
# A relayed message wrapped in tags is another program's text, and its
# sentences were mined as decisions with the tag tail attached.
_MACHINE = re.compile(r"</?[A-Za-z][\w-]*(?:\s[^<>]{0,160})?/?>|\"\s*}|{\s*\"|\\\"|\\n")
# A request opener: asks for something rather than deciding it.
_REQUEST = re.compile(
    r"^\s*(?:(?:can|could|would|will|may) (?:you|we|i)\b|please\b|give me|tell me|show me|let me know|"
    r"what (?:is|are|do|does|should|would)|how (?:do|does|should|would|about)|remind me|help me)",
    re.IGNORECASE,
)
# A one-off instruction for the task at hand: a step or item number, an
# ordinal pick, a time word for this session ("yet", "for later",
# "tomorrow"), an approval of a plan ("go ahead with", "fine with") or a
# verb with a pronoun object after a yes ("ok run it"). Nothing to
# remember once the task is done; a yes plus a hold was a preference
# (2026-09-23).
_ONE_OFF = re.compile(
    r"\b(?:steps?|items?|parts?|phases?|options?|tasks?|points?|bullets?)\s+(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)\b|"
    r"\bthe\s+(?:first|second|third|fourth|fifth|last|next|remaining|other)\s+(?:one|two|three|four|five|few|half|option|item|step|part|bullet)s?\b|"
    r"\b(?:for\s+later|for\s+tomorrow|tomorrow|tonight|in\s+this\s+pass|this\s+pass|report\s+back)\b|"
    r"\byet\b",
    re.IGNORECASE,
)
_ACK_WORDS = (
    r"(?:ok(?:ay)?|yes|yeah|yep|sure|fine|good|great|nice|approved?|confirmed?|go ahead|sounds good|looks good|"
    r"proceed|agreed|alright|all right|accepted)"
)
_ACK_THEN = re.compile(
    r"^(?:" + _ACK_WORDS + r"\b[\s,.!;:-]*)+(?:(?:with|on)\b|(?:and\s+|then\s+|just\s+)?\w+\s+(?:it|them|this|that|these|those)\b)",
    re.IGNORECASE,
)
# A status assessment: "should be" with an evaluative word is a reading of
# the state ("should be good now"), not a rule; "should be logged" is one.
_ASSESSMENT = re.compile(
    r"\b(?:should|ought to|must|will) be (?:all )?(?:good|fine|ok(?:ay)?|ready|done|enough|working|fixed|clean|green|"
    r"safe|set|sorted|right|correct|stable|solid|better|faster)\b",
    re.IGNORECASE,
)
# An address or a secret: an email, a key/token/password with a value, or
# a bare token (16+ letters and digits with no separator, at least three
# of each). A decision that carries one was stored from a prompt that
# pasted an account line (2026-09-24); nothing like that belongs in a
# store that is re-injected into later sessions.
_PRIVATE = re.compile(
    r"[\w.+-]+@[\w-]+\.[A-Za-z]{2,}|"
    r"\b(?:api[_ -]?key|secret|token|password|passwd|bearer)\b\s*[:=]\s*\S{8,}|"
    r"\b(?=[A-Za-z0-9]{16,}\b)(?=(?:[A-Za-z]*\d){3})(?=(?:\d*[A-Za-z]){3})[A-Za-z0-9]{16,}\b",
    re.IGNORECASE,
)
# A report: a table cell, a labelled count, "N passed", two commit hashes.
_REPORT_SHAPE = re.compile(
    r"\s\|\s|^\s*\||\b(?:count|total|passed|failed|errors?|outcomes?|exit(?:ed)?)\s*[:=]\s*\d|"
    r"\b\d+\s+(?:passed|failed|errors?)\b|\b[0-9a-f]{7,40}\b.*\b[0-9a-f]{7,40}\b|"
    r"(?:^|\n)\s*\(?[0-9a-f]{7,40}\)?\s*:",  # a line led by a commit hash
    re.IGNORECASE,
)


def bare(text: str) -> str:
    """The text without windvane's own prefixes. A "(confirmed)" entry is an
    assistant proposal the user said yes to; only its first sentence is the
    proposal, the rest is the report that followed it."""
    raw = (text or "").lstrip()
    confirmed = "(confirmed)" in raw[:40]
    t = _PREFIX.sub("", raw).strip()
    if confirmed:
        t = re.split(r"(?<=[.!?])\s+|\n", t, maxsplit=1)[0].strip()
    return t


def _alpha_ratio(s: str) -> float:
    letters = sum(ch.isalpha() or ch.isspace() for ch in s)
    return letters / max(1, len(s))


def why_not(text: str) -> str:
    """The first reason a text is not a decision, or '' when it may be one
    (the cue is checked by the callers, per kind)."""
    t = bare(text)
    if "?" in t or (_QUESTION_OPENER.match(t) and not _ASSESSMENT.search(t)):
        return "question"  # "should be fine now" is an assessment, judged below
    if len(t) < 12 or len(t.split()) < 3:
        return "too short"
    if _ACK.match(t):
        return "acknowledgement"
    if "\n" in t:
        # A decision is one sentence; the prompt hook passes sentences, the
        # miner passes messages, and a message with a line break inside is
        # a paste (a status block, a relayed report), 2026-09-24. A list
        # under one lead ("use:\n- FastAPI\n- SQLAlchemy") is one choice.
        lines = [ln.strip() for ln in t.split("\n") if ln.strip()]
        if not all(re.match(r"(?:[-*•]|\d+[.)])\s", ln) for ln in lines[1:]):
            return "a multi-line paste"
    if _CODE_START.match(t):
        return "starts like code or a path"
    if _MACHINE.search(t):
        return "markup or machine text"
    if _REQUEST.match(t):
        return "a request"
    if _ONE_OFF.search(t) or _ACK_THEN.match(t):
        return "a one-off instruction"
    if _ASSESSMENT.search(t):
        return "a status assessment"
    if _APPROVAL_WORDS.search(t) and not _CUE.search(_APPROVAL_WORDS.sub(" ", t)):
        return "an approval verdict"
    if _PRIVATE.search(t):
        return "carries an address or a secret"
    if _REPORT_SHAPE.search(t):
        return "count, table or commit report"
    if _alpha_ratio(t) < 0.75:
        return "mostly symbols"
    return ""


def looks_like_decision(text: str) -> bool:
    """A decision: the shape above, not a hedge, plus a deciding word (an
    edit verb counts only with a scope word)."""
    t = bare(text)
    if why_not(text) or _HEDGE.search(t):
        return False
    if "(confirmed)" in (text or "").lstrip()[:40] and not _PROPOSAL.search(t):
        return False
    if _CUE.search(t):
        return True
    if _ACTION_CUE.search(t) and not _ACTION_DEICTIC.search(t):
        return bool(_SCOPE.search(t) or _TRANSITION.search(t))
    return False


def looks_like_correction(text: str) -> bool:
    """A correction or preference: the shape above, plus a redirecting word,
    and not a hedge ("not sure if we need it yet" redirects nothing). Four
    words at least: a three-word preference is a fragment more often than a
    rule (2026-09-24)."""
    t = bare(text)
    if len(t.split()) < 4:
        return False
    return not why_not(text) and bool(_CORRECTION_CUE.search(t)) and not _HEDGE.search(t)


# ===========================================================================
# The regex tier
# ===========================================================================

# Trigger words that the decision regex looks for -- used for typo correction
_DECISION_TRIGGER_WORDS = {
    "switch",
    "use",
    "using",
    "adopt",
    "prefer",
    "replace",
    "migrate",
    "swap",
    "change",
    "convert",
    "upgrade",
    "downgrade",
    "rewrite",
    "refactor",
    "remove",
    "drop",
    "import",
    "implement",
    "build",
    "choose",
    "pick",
    "stick",
    "keep",
    "stop",
    "avoid",
    "never",
    "always",
    "should",
    "please",
    "going",
    "forward",
    "instead",
    "rather",
    "lets",
    "let's",
    "don't",
    "dont",
    "doing",
    "importing",
}


def _fix_typo(word: str) -> str:
    """
    A trigger word with one slip in it comes back as the trigger; anything
    else comes back unchanged. Only the shapes fingers make are corrected
    (an adjacent swap, one letter dropped, one letter doubled), and only on
    words of five letters or more: below that a one-edit neighbour is
    another word ("one" is not a typo of "use", "ever" not of "never",
    "step" not of "stop"), and a rewritten real word garbled a stored
    decision (2026-09-23). A one-letter substitution counts from seven
    letters ("impliment"). There is no edit-distance-2 pass: "chance" is
    not "change" and "remote" is not "remove".
    """
    if word in _DECISION_TRIGGER_WORDS:
        return word
    if len(word) < 5 or word in _COMMON_WORDS:
        return word

    # Adjacent character swaps (most common typo: "swtich" -> "switch")
    for i in range(len(word) - 1):
        swapped = word[:i] + word[i + 1] + word[i] + word[i + 2 :]
        if swapped in _DECISION_TRIGGER_WORDS:
            return swapped

    # One letter dropped ("plase" -> "please")
    for trigger in _DECISION_TRIGGER_WORDS:
        if len(trigger) == len(word) + 1:
            for i in range(len(trigger)):
                if trigger[:i] + trigger[i + 1 :] == word:
                    return trigger

    # One letter extra ("useing" -> "using")
    for i in range(len(word)):
        shorter = word[:i] + word[i + 1 :]
        if shorter in _DECISION_TRIGGER_WORDS:
            return shorter

    # One letter substituted, long words only ("impliment" -> "implement")
    if len(word) >= 7:
        for trigger in _DECISION_TRIGGER_WORDS:
            if len(trigger) == len(word):
                diffs = sum(1 for a, b in zip(word, trigger) if a != b)
                if diffs == 1:
                    return trigger

    return word


# Words the typo corrector must leave alone even when a trigger is one
# edit away ("using" is not "going", "strict" is not "stick").
_COMMON_WORDS = {
    "the",
    "a",
    "an",
    "is",
    "was",
    "are",
    "were",
    "be",
    "been",
    "being",
    "have",
    "has",
    "had",
    "do",
    "does",
    "did",
    "will",
    "would",
    "could",
    "should",
    "may",
    "might",
    "shall",
    "can",
    "need",
    "dare",
    "ought",
    "used",
    "using",
    "to",
    "of",
    "in",
    "for",
    "on",
    "with",
    "at",
    "by",
    "from",
    "up",
    "about",
    "into",
    "through",
    "during",
    "before",
    "after",
    "above",
    "below",
    "between",
    "out",
    "off",
    "over",
    "under",
    "again",
    "further",
    "then",
    "once",
    "here",
    "there",
    "when",
    "where",
    "why",
    "how",
    "all",
    "each",
    "every",
    "both",
    "few",
    "more",
    "most",
    "other",
    "some",
    "such",
    "no",
    "nor",
    "not",
    "only",
    "own",
    "same",
    "so",
    "than",
    "too",
    "very",
    "just",
    "because",
    "but",
    "and",
    "or",
    "if",
    "while",
    "that",
    "this",
    "these",
    "those",
    "i",
    "you",
    "he",
    "she",
    "it",
    "we",
    "they",
    "me",
    "him",
    "her",
    "us",
    "them",
    "my",
    "your",
    "his",
    "its",
    "our",
    "their",
    "what",
    "which",
    "who",
    "whom",
    "going",
    "get",
    "got",
    "make",
    "take",
    "come",
    "go",
    "see",
    "know",
    "think",
    "say",
    "said",
    "like",
    "look",
    "find",
    "give",
    "tell",
    "work",
    "call",
    "try",
    "ask",
    "turn",
    "start",
    "show",
    "hear",
    "play",
    "run",
    "move",
    "live",
    "old",
    "new",
    "good",
    "bad",
    "big",
    "small",
    "long",
    "short",
    "high",
    "low",
    "right",
    "left",
    "sure",
    "still",
    "also",
    "back",
    "well",
    "way",
    "even",
    "want",
    "first",
    "last",
    "next",
    "now",
    "then",
    "end",
    "set",
    "put",
    "point",
    "help",
    "hand",
    "home",
    "any",
    "best",
    "open",
    "much",
    "real",
    "form",
    "part",
    "since",
    "until",
    "along",
    "never",
    "always",
    "stick",
    "strict",
    "script",
    "fast",
    "hard",
    "soft",
}


def _original_words(original: str, normalized: str, start: int, end: int) -> str:
    """The original text's words behind a span of its normalized form.
    _normalize_typos keeps one token per token, so a span maps back by
    token index. What is stored is what the person typed: the lowercased,
    typo-corrected working text never leaves the scorer (a rewritten word
    garbled a stored decision, 2026-09-23)."""
    tokens = original.split()
    first = len(normalized[:start].split())
    if start > 0 and not normalized[start - 1].isspace() and first > 0:
        first -= 1  # the span starts inside a token
    count = max(len(normalized[start:end].split()), 1)
    picked = tokens[first : first + count]
    if not picked:
        return normalized[start:end].strip()
    return " ".join(picked).strip().rstrip(".")


def _normalize_typos(text: str) -> str:
    """Correct trigger-word typos in text for better regex matching."""
    words = text.split()
    corrected = []
    for w in words:
        # Preserve punctuation
        prefix = ""
        suffix = ""
        core = w
        while core and not core[0].isalnum():
            prefix += core[0]
            core = core[1:]
        while core and not core[-1].isalnum():
            suffix = core[-1] + suffix
            core = core[:-1]
        if core:
            fixed = _fix_typo(core.lower())
            # Preserve original case if no fix
            if fixed != core.lower():
                core = fixed
            else:
                core = core.lower()
        corrected.append(prefix + core + suffix)
    return " ".join(corrected)


def _score_decision_intent(text: str) -> tuple[float, str]:
    """
    Score whether a sentence expresses a decision. No LLM needed.

    Uses weighted keyword + sentence structure analysis:
    - Decision verbs (use, switch, adopt, prefer, go with)
    - Directive markers (let's, we should, from now on, always, never)
    - Contrast signals (instead of, rather than, not X but Y, over)
    - Negation constraints (don't, stop, avoid, never)

    Applies lightweight typo correction on trigger words before matching.

    Returns (score 0.0-1.0, extracted_decision_text).
    Score >= 0.5 = capture as decision.
    """
    text_lower = _normalize_typos(text.lower().strip())
    score = 0.0
    best_match = ""

    # --- Decision verb presence (0.35 max) ---
    # Strong verbs that almost always indicate a decision
    strong_verbs = [
        "switch to",
        "go with",
        "adopt",
        "move to",
        "migrate to",
        "replace with",
        "swap to",
        "change to",
        "convert to",
        "upgrade to",
        "downgrade to",
        "rewrite in",
        "refactor to",
        "get rid of",
    ]
    # Moderate verbs that need additional context
    moderate_verbs = [
        "use",
        "prefer",
        "choose",
        "pick",
        "stick with",
        "keep using",
        "implement with",
        "build with",
        "import from",
        "import",
        "replace",
        "remove",
        "drop",
    ]
    verb_score = 0.0
    for verb in strong_verbs:
        if verb in text_lower:
            verb_score = 0.35
            break
    if verb_score == 0:
        for verb in moderate_verbs:
            if verb in text_lower:
                verb_score = 0.25
                break
    score += verb_score

    # --- Directive markers (0.25 max) ---
    directive_patterns = [
        (
            r"\blet'?s\s+(use|switch|go\s+with|change|adopt|move|try|replace|remove|drop|keep|stick|rewrite|migrate|do\s+it)\b",
            0.25,
        ),
        (r"\bwe\s+should\b", 0.25),
        (r"\bfrom\s+now\s+on\b", 0.25),
        (r"\bgoing\s+forward\b", 0.2),
        (r"\balways\s+", 0.2),
        (r"\bnever\s+", 0.2),
        (r"\bmake\s+sure\s+(to|we)\b", 0.15),
        (r"\bi\s+want\s+(to|you\s+to)\b", 0.2),
        (
            r"\bplease\s+(use|switch|change|adopt|go|replace|remove|drop|stop|rewrite)\b",
            0.25,
        ),
        (r"\bgo\s+ahead\s+and\b", 0.2),
        (r"\bjust\s+(use|do|go|switch|replace)\b", 0.2),
    ]
    for pattern, weight in directive_patterns:
        if re.search(pattern, text_lower):
            score += weight
            break

    # --- Contrast/comparison signals (0.2 max) ---
    contrast_patterns = [
        (r"\binstead\s+of\b", 0.2),
        (r"\brather\s+than\b", 0.2),
        (r"\bnot\s+\w+\s+but\b", 0.15),
        (r"\bover\s+\w+", 0.1),
        (r"\binstead\b", 0.1),
        (r"\brather\b", 0.1),
        (r"\breplace\b", 0.15),
    ]
    for pattern, weight in contrast_patterns:
        if re.search(pattern, text_lower):
            score += weight
            break

    # --- Negation constraints (0.2 max) ---
    negation_patterns = [
        (r"\bdon'?t\s+(use|do|add|include|import|ever)\b", 0.2),
        (r"\bstop\s+(using|doing|importing)\b", 0.2),
        (r"\bavoid\s+\w", 0.2),
        (r"\bnever\s+\w", 0.2),
        (r"\bremove\s+(the|all)\b", 0.15),
        (r"\bget\s+rid\s+of\b", 0.2),
        (r"\bdrop\s+(the|all|this)\b", 0.15),
    ]
    for pattern, weight in negation_patterns:
        if re.search(pattern, text_lower):
            score += weight
            break

    # --- A rule stated as one (0.6 floor) ---
    # A directive that OPENS the sentence with a body of four words or
    # more is a rule by form ("from now on every PR needs a test", "always
    # pin dependency versions", "don't use sleep in tests") even when no
    # listed verb follows; mid-sentence the same words are narration as
    # often as a rule. "always been", "never mind", "don't worry" are
    # excluded. On the corpus the regex tier alone kept 32 of 40
    # corrections before this and the shape gate still decides (2026-09-23).
    rule_open = (
        re.match(r"^(?:from\s+now\s+on|going\s+forward)[,:]?\s+\S+(?:\s+\S+){3,}", text_lower)
        or re.match(
            r"^(?:always|never)\s+(?!(?:been|be|was|were|had|has|have|is|are|mind|the|a|an|so|very|really|"
            r"it|that|this|there|again|once|ever|thought|seen|worked|works)\b)\S+(?:\s+\S+){3,}",
            text_lower,
        )
        or re.match(
            r"^(?:don'?t|do\s+not|stop|avoid)\s+(?!(?:worry|know|think|forget|bother|panic|mind|care|remember|see|get)\b)"
            r"\S+(?:\s+\S+){3,}",
            text_lower,
        )
    )
    if rule_open:
        score = max(score, 0.6)

    # --- Penalties ---
    # Questions are not decisions
    if "?" in text:
        score *= 0.3
    # Very short text is probably not a decision
    if len(text_lower) < 25:
        score *= 0.5
    # "can you" / "could you" / "would you" are requests, not decisions
    if re.search(r"\b(can|could|would|should)\s+you\b", text_lower):
        score *= 0.6
    # "what if" / "how about" are exploratory
    if re.search(r"\b(what\s+if|how\s+about|maybe|perhaps)\b", text_lower):
        score *= 0.5

    # --- Extract the decision text ---
    # Try to find the most decision-like sentence/clause
    extraction_patterns = [
        # "let's use X instead of Y"
        r"(let'?s\s+.{10,120}?)(?:\.|$|\n)",
        # "we should X"
        r"(we\s+should\s+.{10,120}?)(?:\.|$|\n)",
        # "from now on X" / "going forward X"
        r"((?:from\s+now\s+on|going\s+forward),?\s+.{10,120}?)(?:\.|$|\n)",
        # "please use/switch/change X"
        r"(please\s+(?:use|switch|change|adopt|go|replace|drop|remove|stop)\s+.{5,120}?)(?:\.|$|\n)",
        # "don't use X" / "avoid X" / "stop using X" / "never X"
        r"((?:don'?t|do\s+not|stop|avoid|never)\s+(?:use|using|do|doing|add|include)\s+.{5,100}?)(?:\.|$|\n)",
        # "use X instead of Y" / "switch to X"
        r"((?:use|switch\s+to|go\s+with|adopt|prefer|replace\s+\w+\s+with)\s+.{5,100}?)(?:\.|$|\n)",
        # "I want to/you to X"
        r"(i\s+want\s+(?:to|you\s+to)\s+.{10,120}?)(?:\.|$|\n)",
        # "always/never X"
        r"((?:always|never)\s+.{10,100}?)(?:\.|$|\n)",
    ]

    original = text.strip()
    for pattern in extraction_patterns:
        match = re.search(pattern, text_lower)
        if match:
            best_match = _original_words(original, text_lower, match.start(1), match.end(1))
            break

    # Fallback: if score is high but no extraction, take first 120 chars
    if score >= 0.5 and not best_match:
        # Take up to first period or newline
        first_sentence = re.split(r"[.\n]", original)[0].strip()
        if len(first_sentence) > 15:
            best_match = first_sentence[:120]

    return (min(score, 1.0), best_match)


def _cut_words(s: str, n: int) -> str:
    """Truncate at a word boundary with an ellipsis — a decision cut
    mid-sentence ("i like your order. well probaly do all if i...") is
    useless when it resurfaces in a banner weeks later."""
    s = " ".join((s or "").split())
    if len(s) <= n:
        return s
    return s[:n].rsplit(" ", 1)[0] + "…"


# ===========================================================================
# The semantic tier
# ===========================================================================


def _resolve_cache_dir() -> Path:
    """Template cache lives in the windvane store (``config.store_dir()``)."""
    from windvane.config import store_dir

    return store_dir() / "embeddings"


# Cache directory for pre-computed embeddings
_CACHE_DIR = _resolve_cache_dir()
_TEMPLATE_CACHE = _CACHE_DIR / "decision_templates.json"

# Decision templates — sentences that express clear decisions.
# These use realistic generic nouns (not X/Y placeholders) because encoders
# embed content semantically — "X" doesn't match "PostgreSQL".
DECISION_TEMPLATES = [
    # Technology/tool switches
    "let's use PostgreSQL instead of SQLite for the database",
    "switch to TypeScript for the frontend components",
    "we should adopt Redis for caching instead of Memcached",
    "go with FastAPI instead of Flask for the API server",
    "replace the old middleware with the new framework",
    "migrate from JavaScript to TypeScript for type safety",
    "rewrite the backend in Go instead of Python",
    "upgrade to the latest version of the library",
    "move to a monorepo structure for the project",
    "let's use Docker for the development environment",
    "switch to using async functions throughout the codebase",
    "I want to use GraphQL instead of REST for the API",
    # Convention/rule decisions
    "from now on always use strict mode in TypeScript files",
    "going forward prefer composition over inheritance",
    "always validate inputs at the API boundary layer",
    "the convention should be snake_case for all Python files",
    "stick with the existing naming conventions for consistency",
    "keep using the current architecture, it works well",
    "prefer functional components over class components",
    # Negation decisions
    "don't use var anymore, use const and let instead",
    "stop using console.log for debugging, use the logger",
    "avoid raw SQL queries, use the ORM instead",
    "never import from the internal package directly",
    "get rid of the old jQuery code and use modern JavaScript",
    "remove the deprecated endpoints from the API",
    "drop support for the legacy database format",
    # Architecture decisions
    "use the repository pattern for data access",
    "implement dependency injection for better testability",
    "separate the concerns into microservices",
    "use a message queue for background processing",
    "add a caching layer between the API and database",
    "refactor to use the event-driven architecture pattern",
]

# Non-decision templates — things that look similar but are NOT decisions.
NON_DECISION_TEMPLATES = [
    "what does this function do and how does it work",
    "can you explain how the authentication system works",
    "fix the bug in the login page handler",
    "there's an error in the database connection code",
    "run the test suite and check for failures",
    "looks good, let's ship it to production",
    "should we use Redis or Memcached for caching",
    "what if we tried using a different framework",
    "how about using GraphQL for this endpoint",
    "maybe we could try a different approach to this",
    "what are the options for the database migration",
    "tell me about the authentication middleware",
    "help me understand the routing configuration",
    "review the changes in the pull request",
    "commit these changes to the main branch",
    "check the error logs for the server crash",
    "where is the configuration file located",
    "how do I set up the development environment",
]

# Minimum similarity to consider a match. Note: the capture cutoff
# (CAPTURE_THRESHOLD) already implies a higher similarity, so this gate
# only binds for the raw-score consumers.
DECISION_THRESHOLD = 0.45
# Minimum gap between best decision and best non-decision score.
# Retuned for bge-base (was 0.05, tuned on MiniLM): 0.025 measured
# F1 72.7% -> 76.9% on the 220-prompt bench (recall 66.7 -> 77.5,
# precision 80.0 -> 76.2) — lost decisions are unrecoverable, noise
# captures get deduped, so the recall side of the trade wins.
AMBIGUITY_MARGIN = 0.025


def _try_import_sentence_transformers():
    """Is sentence-transformers installed? Asked through windvane.semantic,
    which looks the modules up without importing them. True, or None when
    the semantic extra is missing."""
    try:
        from windvane import semantic

        return True if semantic.available() else None
    except Exception:
        return None


def _get_or_build_template_cache() -> Optional[dict]:
    """
    Load cached template embeddings, or build them if missing.
    Returns dict with 'decision_embeddings' and 'non_decision_embeddings' as lists,
    or None if sentence-transformers is not available.
    """
    from windvane.semantic.config import embed_signature, load_sentence_transformer

    sig = embed_signature()

    # Try to load from cache first; a cache built by a different embedding
    # model is invalid (vectors from two models share no space) and rebuilds.
    if _TEMPLATE_CACHE.exists():
        try:
            cache = json.loads(_TEMPLATE_CACHE.read_text())
            # Validate cache has expected keys and correct template count
            if (
                cache.get("decision_count") == len(DECISION_TEMPLATES)
                and cache.get("non_decision_count") == len(NON_DECISION_TEMPLATES)
                and cache.get("model") == sig
            ):
                return cache
        except Exception:
            pass

    # Need to rebuild — requires sentence-transformers
    if _try_import_sentence_transformers() is None:
        return None

    try:
        model = load_sentence_transformer()

        decision_embs = model.encode(DECISION_TEMPLATES, normalize_embeddings=True)
        non_decision_embs = model.encode(NON_DECISION_TEMPLATES, normalize_embeddings=True)

        cache = {
            "model": sig,
            "decision_count": len(DECISION_TEMPLATES),
            "non_decision_count": len(NON_DECISION_TEMPLATES),
            "decision_embeddings": decision_embs.tolist(),
            "non_decision_embeddings": non_decision_embs.tolist(),
            "built_at": time.time(),
        }

        # Save to disk
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        temp = _TEMPLATE_CACHE.with_suffix(".json.tmp")
        temp.write_text(json.dumps(cache))
        temp.replace(_TEMPLATE_CACHE)

        return cache
    except Exception:
        return None


def score_decision_semantic(text: str, server_only: bool = False) -> tuple[float, str]:
    """
    Score whether text expresses a decision using semantic similarity.

    One path: the windvane daemon (~5ms). When no daemon answers,
    (0.0, "") -- the caller's regex tier scores. No process loads the model
    for a prompt; ``server_only`` is kept for the callers that pass it.

    Returns (score 0.0-1.0, extracted_text).
    """
    if len(text.strip()) < 15:
        return (0.0, "")

    # Path 1: the daemon (fastest -- model already loaded)
    try:
        from windvane.daemon import score_via_server

        score, extracted = score_via_server(text)
        if score > 0.0 or extracted:
            return (score, extracted)
        # The daemon returned 0 -- could be a genuine 0 or no daemon.
        # Check whether one is reachable before falling through.
        from windvane.daemon import PORT_FILE

        if PORT_FILE.exists():
            return (score, extracted)  # the daemon is up, the score is genuinely 0
    except Exception:
        pass

    # No daemon answered: the regex tier scores. A hook process never loads
    # the model itself -- that load is ~1.4 GB resident and ~3 GB of commit
    # charge per hook, and it fired on every prompt of every session while
    # no daemon was bound (2026-09-25, under a chain of orphaned daemons).
    del server_only
    return (0.0, "")


def build_template_cache() -> bool:
    """
    Pre-build the template embedding cache. Call during install.
    Returns True if successful, False if sentence-transformers not available.
    """
    cache = _get_or_build_template_cache()
    return cache is not None


# ===========================================================================
# The one judgement
# ===========================================================================

# The capture cutoff every path shares: the best of the semantic and the
# regex tier has to reach this before the shape gate is consulted.
CAPTURE_THRESHOLD = 0.6
# Stored decisions keep the matched sentence to this many characters, cut
# at a word.
CAPTURE_MAX_CHARS = 300


def capture_decision(text: str, server_only: bool = False) -> str:
    """The one judgement behind every decision capture. The prompt hook
    (a typed prompt, live) and the session miner (a correction found in a
    transcript, bootstrap and live ticks) both call this, so a sentence is
    stored or not by the same rule wherever it was seen. When the two paths
    kept separate rules they drifted (2026-09-23: the miner's preference
    path was the last leak, about half of its entries real).

    Tiers, best score wins: the semantic scorer (the daemon), then the
    regex scorer over each sentence. The winner has to reach
    CAPTURE_THRESHOLD and pass the shape gate (looks_like_decision).
    Returns the sentence to store, cut at a word to CAPTURE_MAX_CHARS, or
    "" when nothing qualifies. Never raises.
    """
    try:
        prompt = (text or "").strip()
        if len(prompt) < 15:
            return ""
        best_score, best_text = 0.0, ""
        try:
            best_score, best_text = score_decision_semantic(prompt, server_only=server_only)
        except Exception:
            best_score, best_text = 0.0, ""

        sentences = [s.strip() for s in re.split(r"(?<=[.!])\s+|\n+", prompt) if len(s.strip()) > 15]
        if len(sentences) <= 1:
            sentences = [prompt]
        for sentence in sentences:
            regex_score, regex_text = _score_decision_intent(sentence)
            if regex_score > best_score:
                best_score, best_text = regex_score, regex_text

        if best_score < CAPTURE_THRESHOLD or not best_text or len(best_text) < 15:
            return ""
        if not looks_like_decision(best_text):
            return ""
        return _cut_words(best_text, CAPTURE_MAX_CHARS)
    except Exception:
        return ""
