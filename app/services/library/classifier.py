"""Rule-based document categorisation.

Deliberately simple and explainable: a category is suggested when its
characteristic words appear in the document, and the words that decided it
are returned as evidence. No model, no network, nothing learned from the
user's documents. A suggestion is never treated as a fact; the user can
override it and the override is never overwritten.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

ENGINE = "ard-rules/1"

CATEGORIES: dict[str, str] = {
    "invoices_receipts": "Invoices and receipts",
    "contracts": "Contracts and agreements",
    "education": "Education and certificates",
    "business": "Business and operations",
    "government": "Government documents",
    "identity": "Identity documents",
    "books_research": "Books and research",
    "forms_applications": "Forms and applications",
    "tax_finance": "Tax and finance",
    "medical_insurance": "Medical and insurance",
    "reports_presentations": "Reports and presentations",
    "uncategorized": "Uncategorized",
}

# (phrase, weight). Phrases are matched as whole words, case-insensitively. Hindi terms are included
# for the categories where they are common on Indian documents.
_RULES: dict[str, list[tuple[str, float]]] = {
    "invoices_receipts": [("invoice", 3), ("tax invoice", 3), ("receipt", 2.5), ("bill to", 2.5), ("amount due", 2.5),
                          ("invoice number", 3), ("subtotal", 2), ("total due", 2), ("gstin", 2), ("qty", 1),
                          ("unit price", 2), ("payment terms", 1.5), ("purchase order", 1.5), ("चालान", 3),
                          ("रसीद", 3), ("बिल", 1.5), ("कुल राशि", 2)],
    "contracts": [("agreement", 2.5), ("contract", 2), ("hereinafter", 3), ("whereas", 2), ("party of the", 2),
                  ("the parties", 2), ("terms and conditions", 1.5), ("governing law", 3), ("termination", 1.5),
                  ("indemnify", 2.5), ("witnesseth", 3), ("lease", 2), ("non-disclosure", 3), ("अनुबंध", 3),
                  ("समझौता", 3), ("पक्षकार", 2)],
    "education": [("certificate", 2), ("transcript", 3), ("diploma", 3), ("degree", 1.5), ("university", 2),
                  ("marksheet", 3), ("mark sheet", 3), ("semester", 2), ("examination", 2), ("grade", 1), ("cgpa", 3),
                  ("roll number", 2.5), ("school", 1.5), ("प्रमाण पत्र", 3), ("अंकतालिका", 3), ("विश्वविद्यालय", 2.5)],
    "business": [("minutes of meeting", 3), ("memorandum", 2), ("standard operating procedure", 3), ("policy", 1),
                 ("board of directors", 2.5), ("stakeholders", 1.5), ("deliverables", 2), ("project plan", 2.5),
                 ("meeting agenda", 2.5), ("action items", 2), ("purchase requisition", 2)],
    "government": [("government of", 3), ("ministry of", 3), ("department of", 1.5), ("gazette", 3), ("notification", 1.5),
                   ("act,", 1), ("municipal", 2), ("licence", 1.5), ("license", 1.5), ("registration certificate", 2),
                   ("भारत सरकार", 3), ("मंत्रालय", 3), ("सरकार", 2), ("अधिसूचना", 2.5)],
    "identity": [("passport", 3), ("date of birth", 2), ("aadhaar", 3), ("permanent account number", 3),
                 ("driving licence", 3), ("driving license", 3), ("voter", 2), ("identity card", 3), ("nationality", 2),
                 ("आधार", 3), ("जन्म तिथि", 2.5), ("पहचान पत्र", 3), ("मतदाता", 2.5)],
    "books_research": [("abstract", 2.5), ("introduction", 1), ("references", 1.5), ("bibliography", 3), ("et al", 2.5),
                       ("doi", 2), ("chapter", 1.5), ("isbn", 3), ("keywords", 1.5), ("methodology", 2),
                       ("literature review", 3), ("journal of", 2.5), ("proceedings", 2)],
    "forms_applications": [("application form", 3), ("applicant", 2), ("please fill", 2.5), ("signature of", 2),
                           ("for office use", 3), ("tick the", 2), ("block letters", 2.5), ("declaration", 1.5),
                           ("enclosures", 1.5), ("आवेदन", 3), ("आवेदक", 2.5), ("हस्ताक्षर", 2)],
    "tax_finance": [("income tax", 3), ("assessment year", 3), ("tax return", 3), ("bank statement", 3),
                    ("account statement", 2.5), ("balance sheet", 3), ("profit and loss", 3), ("form 16", 3),
                    ("tds", 2), ("opening balance", 2.5), ("closing balance", 2.5), ("ifsc", 2), ("आयकर", 3),
                    ("बैंक विवरण", 3)],
    "medical_insurance": [("patient", 2.5), ("diagnosis", 3), ("prescription", 3), ("hospital", 2), ("discharge summary", 3),
                          ("policy number", 2), ("insured", 2.5), ("premium", 1.5), ("claim form", 2.5), ("laboratory", 1.5),
                          ("dosage", 2.5), ("sum insured", 3), ("रोगी", 2.5), ("बीमा", 3), ("अस्पताल", 2.5)],
    "reports_presentations": [("executive summary", 3), ("annual report", 3), ("quarterly report", 3), ("findings", 1.5),
                              ("recommendations", 1.5), ("table of contents", 2), ("appendix", 1.5), ("agenda", 1),
                              ("key takeaways", 2.5), ("overview", 1), ("prepared by", 1.5)],
}

_COMPILED: dict[str, list[tuple[str, float, re.Pattern[str]]]] = {
    category: [(phrase, weight, re.compile(
        (r"(?<![\w])" + re.escape(phrase) + r"(?![\w])") if phrase.isascii() else re.escape(phrase), re.IGNORECASE))
        for phrase, weight in rules]
    for category, rules in _RULES.items()
}

MIN_SCORE = 4.0
TEXT_LIMIT = 30_000


@dataclass(frozen=True)
class Suggestion:
    category: str
    confidence: float
    evidence: list[dict[str, Any]]

    @property
    def label(self) -> str:
        return CATEGORIES[self.category]


def classify(text: str, name: str = "") -> Suggestion:
    """Suggest a category for a document from its text and its name."""
    sample = (text or "")[:TEXT_LIMIT]
    scores: dict[str, float] = {}
    evidence: dict[str, list[dict[str, Any]]] = {}
    for category, rules in _COMPILED.items():
        total = 0.0
        found = []
        for phrase, weight, pattern in rules:
            in_text = len(pattern.findall(sample))
            in_name = 1 if name and pattern.search(name) else 0
            if not in_text and not in_name:
                continue
            # Repeats count, but with diminishing effect, so one frequent word cannot decide alone.
            total += weight * (min(in_text, 3) ** 0.5 if in_text else 0) + weight * 1.5 * in_name
            found.append({"phrase": phrase, "count": in_text, "in_name": bool(in_name)})
        if found:
            scores[category] = total
            evidence[category] = sorted(found, key=lambda item: -item["count"])[:6]

    if not scores:
        return Suggestion("uncategorized", 0.0, [])
    ranked = sorted(scores.items(), key=lambda item: -item[1])
    best, best_score = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else 0.0
    if best_score < MIN_SCORE:
        return Suggestion("uncategorized", 0.0, [])
    # High when the winner is strong and clearly ahead of the runner-up.
    confidence = round(min(0.99, (best_score / (best_score + second + 3.0)) * min(1.0, best_score / 10.0 + 0.4)), 2)
    return Suggestion(best, confidence, evidence[best])
