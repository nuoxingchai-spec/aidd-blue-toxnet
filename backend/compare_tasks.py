"""Narrow, reviewed TxGemma task contracts; no inferred cross-task equivalence.

The one executable prompt is quoted from Google's public Apache-2.0 cookbook.
The gated snapshot's prompt must agree before local inference is enabled.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re

TXGEMMA_MODEL_ID = "google/txgemma-2b-predict"
TXGEMMA_REVISION = "6b0eb81135ad9e73c9c18409c03e315a5a23a18f"
TASK_CONTRACT = "aidd.txgemma.reviewed-tdc-tasks.v1"
COOKBOOK_REVISION = "461e9f6fd44015171ca0efdbbfe7979427da8ea3"
COOKBOOK_SHA256 = "1a965d8b92428b89ac8174213d5bec157bdf3b1fa1584a8c112e73e9e76c9c82"
COOKBOOK_SOURCE = (
    "https://github.com/google-gemini/gemma-cookbook/blob/" + COOKBOOK_REVISION
    + "/TxGemma/%5BTxGemma%5DQuickstart_with_Hugging_Face.ipynb"
)
# Source: the notebook's printed BBB_Martins prompt, replacing ONLY its example
# SMILES by the notebook's literal input_type placeholder. Apache-2.0 attribution
# and upstream source/revision are retained above and in the review document.
BBB_TEMPLATE = (
    "Instructions: Answer the following question about drug properties.\n"
    "Context: As a membrane separating circulating blood and brain extracellular fluid, "
    "the blood-brain barrier (BBB) is the protection layer that blocks most foreign drugs. "
    "Thus the ability of a drug to penetrate the barrier to deliver to the site of action "
    "forms a crucial challenge in development of drugs for central nervous system.\n"
    "Question: Given a drug SMILES string, predict whether it\n"
    "(A) does not cross the BBB (B) crosses the BBB\n"
    "Drug SMILES: {Drug SMILES}\nAnswer:\n"
)


def normalized_template(value: str) -> str:
    """Ignore only CRLF and print()'s terminal newline, never prompt content."""
    return value.replace("\r\n", "\n").rstrip("\n") + "\n"


@dataclass(frozen=True)
class TdcTask:
    id: str
    name: str
    positive_class: str | None
    supported: bool
    reason: str
    template: str | None = None
    toxnet_task: str | None = None

    @property
    def template_sha256(self):
        return hashlib.sha256(normalized_template(self.template).encode()).hexdigest() if self.template else None

    def public(self):
        return {"id": self.id, "name": self.name, "tdcTask": self.name,
                "supported": self.supported, "status": "supported" if self.supported else "unsupported",
                "inputType": "Drug SMILES", "outputType": "categorical-text",
                "positiveClass": self.positive_class, "directComparable": False,
                "toxnetTask": self.toxnet_task, "reason": self.reason,
                "sourceUrl": COOKBOOK_SOURCE if self.supported else "https://huggingface.co/" + TXGEMMA_MODEL_ID,
                "templateSha256": self.template_sha256, "contractVersion": TASK_CONTRACT,
                "parser": "exact-A-or-B-no-probability.v1" if self.supported else None}


_TASKS = (
    TdcTask("tdc-bbb-martins", "BBB_Martins", "B: crosses the BBB", True,
            "BBB penetration is not one of ToxNet's four toxicity endpoints; no direct accuracy comparison.", BBB_TEMPLATE),
    TdcTask("tdc-herg", "hERG", None, False,
            "Gated official template and assay threshold have not been reviewed; unsupported.", toxnet_task="herg"),
    TdcTask("tdc-dili", "DILI", None, False,
            "Gated official template and label contract have not been reviewed; unsupported.", toxnet_task="dili"),
    TdcTask("tdc-ames", "AMES", None, False,
            "Gated official template and label contract have not been reviewed; unsupported.", toxnet_task="ames"),
    TdcTask("tdc-clintox", "ClinTox", None, False,
            "Gated official template and task selection have not been reviewed; unsupported.", toxnet_task="clintox"),
)


def task_registry():
    return [task.public() for task in _TASKS]


def get_task(task_id):
    for task in _TASKS:
        if task.id == task_id:
            return task
    raise ValueError("Unknown TxGemma task; only registered task IDs are permitted.")


def parse_task_output(task_id: str, output: str):
    task = get_task(task_id)
    if not task.supported:
        raise ValueError("Unsupported TxGemma task.")
    if not isinstance(output, str) or len(output) > 256:
        raise ValueError("TxGemma output is not a bounded categorical answer.")
    answer = output.strip()
    # No free-form rationale parsing, digit extraction, softmax, or probability
    # reinterpretation. A short official option label may accompany A/B.
    match = re.fullmatch(r"(?:([AB])|\(([AB])\))(?:\s*[:.-]?\s*(does not cross the BBB|crosses the BBB))?[.!]?", answer)
    if not match:
        raise ValueError("TxGemma answer is outside the reviewed A/B contract.")
    bare_letter, parenthesized_letter, label = match.groups()
    letter = bare_letter or parenthesized_letter
    expected = "does not cross the BBB" if letter == "A" else "crosses the BBB"
    if label and label != expected:
        raise ValueError("TxGemma option and label contradict each other.")
    return {"taskId": task.id, "tdcTask": task.name, "outputType": "categorical-text",
            "class": letter, "classLabel": expected, "positive": letter == "B",
            "probability": None, "directComparable": False,
            "taskContractVersion": TASK_CONTRACT, "templateSha256": task.template_sha256}
