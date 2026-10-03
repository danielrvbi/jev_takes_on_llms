import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path


HARD_CASE_DIR = Path(__file__).resolve().parent
DATA_DIR = HARD_CASE_DIR / "data"
COMPACT_DIR = HARD_CASE_DIR / "compact"
PACKET_JSON_BUDGET = 50 * 1024
SOURCE_FILENAMES = (
    "policy_wording.md",
    "fnol.md",
    "customer_emails.md",
    "adjuster_notes.md",
    "repair_invoices.md",
    "police_report.md",
    "previous_claims.md",
    "internal_guidelines.md",
    "timeline.md",
)


@dataclass(frozen=True)
class ClaimPacket:
    text: str
    source_filenames: tuple[str, ...] = SOURCE_FILENAMES
    profile: str = "compact"
    original_source_sha256: str | None = None
    source_map_sha256: str | None = None

    def metadata(self):
        if self.profile != "compact":
            raise ValueError("Only compact claim packets are supported")
        metadata = {
            "source_filenames": list(self.source_filenames),
            "sha256": hashlib.sha256(self.text.encode("utf-8")).hexdigest(),
            "character_count": len(self.text),
            "word_count": len(self.text.split()),
            "utf8_byte_count": len(self.text.encode("utf-8")),
        }
        metadata.update(profile=self.profile, original_source_sha256=self.original_source_sha256,
                        compact_packet_sha256=metadata["sha256"],
                        source_map_sha256=self.source_map_sha256,
                        json_encoded_bytes=len(json.dumps(self.text, ensure_ascii=False).encode("utf-8")))
        return metadata


def _original_source_sha256(directory):
    """Hash the original ordered bytes for provenance; never return model input."""
    assert "README.md" not in SOURCE_FILENAMES, "Designer README must never be loaded"
    directory = Path(directory)
    parts = []
    for name in SOURCE_FILENAMES:
        content = (directory / name).read_bytes().decode("utf-8")
        parts.append(f"===== {name} =====\n{content}")
    text = "\n\n".join(parts)
    assert "===== README.md =====" not in text, "Designer README must never be included"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_claim_packet(directory=DATA_DIR, *, profile="compact", compact_directory=COMPACT_DIR):
    """Return only the reviewed compact input, validated against its original sources."""
    if profile != "compact":
        raise ValueError("Only the compact input profile is supported")
    original_sha256 = _original_source_sha256(directory)
    return _load_compact_packet(directory, compact_directory, original_sha256)


def compact_records(text):
    """Parse the checked-in evidence records and their original line references."""
    sections = re.split(r"^===== (.+) =====\n", text, flags=re.MULTILINE)
    if sections[0] or tuple(sections[1::2]) != SOURCE_FILENAMES:
        raise ValueError("Compact packet must contain exactly the nine ordered document sections")
    records = {}
    for name, body in zip(sections[1::2], sections[2::2]):
        for line in body.splitlines():
            if not line:
                continue
            match = re.fullmatch(r"([A-Z]{2}\d{2}) \| L(\d+)-(\d+) \| (.+)", line)
            if not match or match[1] in records:
                raise ValueError(f"Invalid or duplicate compact record in {name}")
            start, end = int(match[2]), int(match[3])
            if start < 1 or end < start:
                raise ValueError("Invalid compact source line range")
            records[match[1]] = {"source": name, "lines": [start, end]}
    return records


def _load_compact_packet(directory, compact_directory, original_sha256):
    compact_directory = Path(compact_directory)
    text = (compact_directory / "packet.md").read_bytes().decode("utf-8")
    map_bytes = (compact_directory / "source_map.json").read_bytes()
    source_map = json.loads(map_bytes.decode("utf-8"))
    assert "===== README.md =====" not in text, "Designer README must never be included"
    if source_map.get("original_source_sha256") != original_sha256:
        raise ValueError("Compact artifact invalid: original source content changed; review and rebuild it")
    if source_map.get("compact_packet_sha256") != hashlib.sha256(text.encode("utf-8")).hexdigest():
        raise ValueError("Compact artifact invalid: packet hash differs from reviewed source map")
    records = compact_records(text)
    if records != source_map.get("records"):
        raise ValueError("Compact artifact invalid: provenance records differ from source map")
    if list(source_map.get("sources", {})) != list(SOURCE_FILENAMES):
        raise ValueError("Compact source map must contain exactly the ordered source allowlist")
    for name in SOURCE_FILENAMES:
        data = (Path(directory) / name).read_bytes()
        lines = data.decode("utf-8").splitlines()
        info = source_map["sources"][name]
        if info != {"sha256": hashlib.sha256(data).hexdigest(), "line_count": len(lines)}:
            raise ValueError(f"Compact artifact invalid: source identity changed: {name}")
        covered = set()
        for record in records.values():
            if record["source"] == name:
                start, end = record["lines"]
                if end > len(lines):
                    raise ValueError(f"Provenance range exceeds original document: {name}")
                covered.update(range(start, end + 1))
        # Headings and empty separators need no evidence record; every substantive line does.
        missing = [index for index, line in enumerate(lines, 1)
                   if line.strip() and not line.startswith("#") and index not in covered]
        if missing:
            raise ValueError(f"Compact provenance omits source lines: {name}: {missing}")
    if len(json.dumps(text, ensure_ascii=False).encode("utf-8")) > PACKET_JSON_BUDGET:
        raise ValueError("Compact packet exceeds 50 KiB JSON-encoded budget; revise it, never truncate")
    return ClaimPacket(text, original_source_sha256=original_sha256,
                       source_map_sha256=hashlib.sha256(map_bytes).hexdigest())
