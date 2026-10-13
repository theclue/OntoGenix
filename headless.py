"""Bounded, consent-scoped OntoGenix bottom-up candidate entry point."""

import argparse
import csv
import hashlib
import io
import json
import os
import re
import stat
import sys
import time
from pathlib import Path, PurePosixPath


MAX_REQUEST_BYTES = 65_536
MAX_SOURCE_BYTES = 16 * 1024 * 1024
MAX_PROFILE_BYTES = 32 * 1024
MAX_CANDIDATE_BYTES = 4 * 1024 * 1024
MAX_RESULT_BYTES = 64 * 1024
MAX_ROWS = 100
MAX_COLUMNS = 64
PROMPT_PURPOSE = "bottom-up-candidate-generation"
SAFE_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
SHA256 = re.compile(r"[a-f0-9]{64}\Z")


class InvalidRequest(ValueError):
    """The caller request or its authorization metadata is invalid."""


def _closed_object(value, keys, label):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise InvalidRequest(f"{label} must contain exactly the documented fields")


def _unique_json_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise InvalidRequest("JSON objects must not contain duplicate keys")
        value[key] = item
    return value


def _nonempty_string(value, label):
    if not isinstance(value, str) or not value.strip():
        raise InvalidRequest(f"{label} must be a non-empty string")


def _safe_relative_path(value, label):
    _nonempty_string(value, label)
    if "\\" in value or ":" in value or value.startswith("/"):
        raise InvalidRequest(f"{label} must be an unambiguous relative path")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise InvalidRequest(f"{label} must not contain traversal or ambiguous components")
    if PurePosixPath(value).as_posix() != value:
        raise InvalidRequest(f"{label} must be normalized")
    return parts


def _positive_int(value, label, maximum):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= maximum:
        raise InvalidRequest(f"{label} is outside the supported positive-integer range")


def _path_without_symlinks(root, relative, label, expect_directory=None):
    current = root
    parts = _safe_relative_path(relative, label)
    for index, part in enumerate(parts):
        current = current / part
        try:
            info = current.lstat()
        except OSError as error:
            raise InvalidRequest(f"{label} does not exist or cannot be inspected") from error
        if stat.S_ISLNK(info.st_mode):
            raise InvalidRequest(f"{label} must not traverse symlinks")
        if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise InvalidRequest(f"{label} has a non-directory parent")
    if expect_directory is True and not stat.S_ISDIR(info.st_mode):
        raise InvalidRequest(f"{label} must be a directory")
    if expect_directory is False and not stat.S_ISREG(info.st_mode):
        raise InvalidRequest(f"{label} must be a regular file")
    return current


def validate_request(request, output_argument, project_root):
    """Validate the closed native request and bind it to verified local paths."""
    _closed_object(request, {
        "contract_version", "run_id", "session_output_path", "operation",
        "approved_scope_ref", "approved_cq_ids", "source", "project",
        "disclosure_authorization",
    }, "request")
    if type(request["contract_version"]) is not int or request["contract_version"] != 1:
        raise InvalidRequest("unsupported request contract version")
    run_id = request["run_id"]
    if not isinstance(run_id, str) or not SAFE_RUN_ID.fullmatch(run_id):
        raise InvalidRequest("run_id is not a safe caller-issued identifier")
    if request["operation"] != "bottom-up-candidate":
        raise InvalidRequest("unsupported operation")
    _nonempty_string(request["approved_scope_ref"], "approved_scope_ref")
    cqs = request["approved_cq_ids"]
    if (not isinstance(cqs, list) or not cqs or any(not isinstance(cq, str) or not cq.strip() for cq in cqs)
            or len(set(cqs)) != len(cqs)):
        raise InvalidRequest("approved_cq_ids must be a non-empty unique string list")

    expected_session = f"ontology/.ontology-sessions/{run_id}/ontogenix"
    if request["session_output_path"] != expected_session:
        raise InvalidRequest("session_output_path does not match the caller-issued run_id")
    ontology_dir = project_root / "ontology"
    try:
        ontology_stat = ontology_dir.lstat()
    except OSError as error:
        raise InvalidRequest("the adopted ontology directory must already exist") from error
    if stat.S_ISLNK(ontology_stat.st_mode) or not stat.S_ISDIR(ontology_stat.st_mode):
        raise InvalidRequest("the adopted ontology path must be a real directory")
    output_path = _path_without_symlinks(
        project_root, expected_session, "session_output_path", expect_directory=True
    )
    if output_path != project_root / expected_session:
        raise InvalidRequest("session output path is not canonical")
    output_arg = Path(output_argument)
    if not output_arg.is_absolute() or output_arg != output_path:
        raise InvalidRequest("--output-dir must equal the pre-authorized session path")
    if output_arg.resolve(strict=True) != output_path:
        raise InvalidRequest("--output-dir resolves through an unexpected path")
    output_info = output_path.stat()
    if output_info.st_uid != os.geteuid() or output_info.st_mode & 0o077:
        raise InvalidRequest("session output directory must be private and caller-owned")
    if any(output_path.iterdir()):
        raise InvalidRequest("the pre-authorized session directory must be empty")

    source = request["source"]
    _closed_object(source, {"source_id", "path", "sha256"}, "source")
    _nonempty_string(source["source_id"], "source.source_id")
    source_parts = _safe_relative_path(source["path"], "source.path")
    if not source_parts[-1].lower().endswith(".csv"):
        raise InvalidRequest("source.path must identify a CSV file")
    if not isinstance(source["sha256"], str) or not SHA256.fullmatch(source["sha256"]):
        raise InvalidRequest("source.sha256 must be a lowercase SHA-256 digest")
    source_path = _path_without_symlinks(
        project_root, source["path"], "source.path", expect_directory=False
    )
    if source_path.stat().st_size > MAX_SOURCE_BYTES:
        raise InvalidRequest("CSV exceeds the 16 MiB dataset limit")

    project = request["project"]
    _closed_object(project, {"ontology_iri", "prefix", "target_profile"}, "project")
    for key in ("ontology_iri", "prefix"):
        _nonempty_string(project[key], f"project.{key}")
    if project["target_profile"] not in ("OWL2_EL", "OWL2_DL"):
        raise InvalidRequest("project.target_profile must be OWL2_EL or OWL2_DL")

    authorization = request["disclosure_authorization"]
    _closed_object(authorization, {
        "authorization_ref", "authorized_by", "purpose", "model_alias", "source_refs",
        "max_requests", "max_input_bytes_per_request", "max_total_input_bytes",
        "max_output_tokens_total", "deadline_seconds",
    }, "disclosure_authorization")
    for key in ("authorization_ref", "authorized_by", "model_alias"):
        _nonempty_string(authorization[key], f"disclosure_authorization.{key}")
    if authorization["purpose"] != PROMPT_PURPOSE:
        raise InvalidRequest("disclosure purpose does not authorize bottom-up candidate generation")
    refs = authorization["source_refs"]
    if not isinstance(refs, list) or len(refs) != 1 or refs[0] != {
        "source_id": source["source_id"], "sha256": source["sha256"]
    }:
        raise InvalidRequest("disclosure source_refs must bind the selected source and digest")
    for key, ceiling in (
        ("max_requests", 4), ("max_input_bytes_per_request", MAX_REQUEST_BYTES),
        ("max_total_input_bytes", 4 * MAX_REQUEST_BYTES),
        ("max_output_tokens_total", 4096), ("deadline_seconds", 300),
    ):
        _positive_int(authorization[key], f"disclosure_authorization.{key}", ceiling)
    return {
        "source_path": source_path,
        "output_path": output_path,
        "authorization": authorization,
        "request": request,
    }


def _read_source(path, expected_digest):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_SOURCE_BYTES:
            raise InvalidRequest("selected source is not a bounded regular file")
        chunks = []
        size = 0
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, MAX_SOURCE_BYTES + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_SOURCE_BYTES:
                raise InvalidRequest("CSV exceeds the 16 MiB dataset limit")
            chunks.append(chunk)
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
        ):
            raise InvalidRequest("selected source changed while it was being read")
    finally:
        os.close(descriptor)
    if digest.hexdigest() != expected_digest:
        raise InvalidRequest("selected source digest does not match the authorized digest")
    return b"".join(chunks), digest.hexdigest()


def _write_private_file(directory, filename, content):
    path = directory / filename
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
    except BaseException:
        raise
    return path


def _profile_csv(csv_bytes):
    if csv_bytes.startswith(b"\xef\xbb\xbf"):
        encoding = "utf-8-sig"
    elif csv_bytes.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        encoding = "utf-32"
    elif csv_bytes.startswith((b"\xff\xfe", b"\xfe\xff")):
        encoding = "utf-16"
    else:
        encoding = "utf-8"
    try:
        text = csv_bytes.decode(encoding)
    except UnicodeError as error:
        raise RuntimeError("CSV encoding is unsupported; UTF-8 or BOM-marked Unicode is required") from error
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample)
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text, newline=""), dialect)
    try:
        headers = next(reader)
    except StopIteration as error:
        raise RuntimeError("CSV has no header row") from error
    if not headers or len(headers) > MAX_COLUMNS:
        raise RuntimeError("CSV must have between 1 and 64 columns")
    columns = [{"name": name, "observed_types": set(), "non_empty_rows": 0, "_unique": set()}
               for name in headers]
    sampled_rows = 0
    truncated = False
    for row in reader:
        if not row:
            continue
        if sampled_rows == MAX_ROWS:
            truncated = True
            break
        if len(row) > MAX_COLUMNS or len(row) != len(headers):
            raise RuntimeError("CSV rows must have a consistent width of at most 64 columns")
        sampled_rows += 1
        for column, value in zip(columns, row):
            value = value.strip()
            if not value:
                continue
            column["non_empty_rows"] += 1
            if value.lower() in ("true", "false"):
                value_type = "boolean"
            else:
                try:
                    int(value)
                    value_type = "integer"
                except ValueError:
                    try:
                        float(value)
                        value_type = "number"
                    except ValueError:
                        value_type = "text"
            column["observed_types"].add(value_type)
            column["_unique"].add(value)
    profile = {
        "rows_profiled": sampled_rows,
        "row_profile_truncated": truncated,
        "columns": [
            {
                "name": column["name"],
                "observed_types": sorted(column["observed_types"]),
                "non_empty_rows": column["non_empty_rows"],
                "unique_values_in_profile": len(column["_unique"]),
            }
            for column in columns
        ],
    }
    try:
        profile_bytes = json.dumps(profile, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except UnicodeError as error:
        raise RuntimeError("CSV metadata is not valid UTF-8") from error
    if len(profile_bytes) > MAX_PROFILE_BYTES:
        raise RuntimeError("bounded data profile exceeds 32 KiB")
    return profile_bytes


class ScopedDisclosure:
    """Authorize only the exact bottom-up prompt and account for API budgets."""

    def __init__(self, authorization, model_alias, expected_messages, source_check, started_at=None):
        self.authorization = authorization
        self.model_alias = model_alias
        self.expected_messages = expected_messages
        self.source_check = source_check
        self.started_at = time.monotonic() if started_at is None else started_at
        self.request_count = 0
        self.input_bytes = 0
        self.output_tokens = 0

    def authorize(self, messages, purpose, routed_model, requested_max_tokens):
        auth = self.authorization
        if purpose != "model request" or routed_model != self.model_alias or messages != self.expected_messages:
            raise PermissionError("model request does not match the authorized purpose, route, and disclosure")
        self.source_check()
        if self.request_count >= auth["max_requests"]:
            raise PermissionError("authorized model request budget is exhausted")
        remaining_time = auth["deadline_seconds"] - (time.monotonic() - self.started_at)
        remaining_tokens = auth["max_output_tokens_total"] - self.output_tokens
        if remaining_time <= 0 or remaining_tokens <= 0:
            raise PermissionError("authorized model deadline or output-token budget is exhausted")
        max_tokens = remaining_tokens if requested_max_tokens is None else min(requested_max_tokens, remaining_tokens)
        serialized = json.dumps({
            "model": routed_model, "messages": messages, "temperature": 0,
            "max_tokens": max_tokens, "stream": False,
        }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(serialized) > auth["max_input_bytes_per_request"]:
            raise PermissionError("authorized per-request input-byte budget is exceeded")
        if self.input_bytes + len(serialized) > auth["max_total_input_bytes"]:
            raise PermissionError("authorized total input-byte budget is exceeded")
        self.request_count += 1
        self.input_bytes += len(serialized)
        return {"max_tokens": max_tokens, "timeout": remaining_time}

    def record_completion(self, completion):
        usage = getattr(completion, "usage", None)
        completion_tokens = getattr(usage, "completion_tokens", None)
        if isinstance(completion_tokens, bool) or not isinstance(completion_tokens, int) or completion_tokens < 0:
            raise RuntimeError("provider did not report actual completion-token usage")
        if self.output_tokens + completion_tokens > self.authorization["max_output_tokens_total"]:
            raise RuntimeError("provider response exceeded the authorized output-token budget")
        if time.monotonic() - self.started_at > self.authorization["deadline_seconds"]:
            raise RuntimeError("provider response exceeded the authorized deadline")
        self.output_tokens += completion_tokens


def _load_model_resources():
    from GUI.GuiManager.metadata import MetadataManager

    metadata = MetadataManager()
    instructions_path = Path(metadata.onto_metadata["ontology_instructions"])
    instruction_bytes = instructions_path.read_bytes()
    instructions = instruction_bytes.decode("utf-8")
    return metadata, instructions, hashlib.sha256(instruction_bytes).hexdigest()


def run_request(request_text, output_argument):
    started_at = time.monotonic()
    try:
        request = json.loads(request_text, object_pairs_hook=_unique_json_object)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise InvalidRequest("stdin must contain one valid UTF-8 JSON request") from error
    if not isinstance(request, dict):
        raise InvalidRequest("request must be a JSON object")
    project_root = Path.cwd().resolve(strict=True)
    bound = validate_request(request, output_argument, project_root)
    source_bytes, source_digest = _read_source(bound["source_path"], request["source"]["sha256"])

    # The native preprocessing path can rewrite CSV input, so only this private copy is used.
    _write_private_file(bound["output_path"], "input-copy.csv", source_bytes)
    copied_source, copied_digest = _read_source(
        bound["output_path"] / "input-copy.csv", request["source"]["sha256"]
    )
    if copied_digest != source_digest:
        raise RuntimeError("private CSV copy digest differs from the authorized source")
    profile_bytes = _profile_csv(copied_source)
    profile_digest = hashlib.sha256(profile_bytes).hexdigest()

    metadata, instructions, resource_digest = _load_model_resources()
    auth = bound["authorization"]
    if metadata.api_model != auth["model_alias"]:
        raise InvalidRequest("authorized model_alias does not match the operator-resolved model alias")
    llm_metadata = dict(metadata.onto_metadata)
    if llm_metadata["model"] != auth["model_alias"]:
        raise InvalidRequest("the configured model route does not match the authorization")
    role = llm_metadata["role"]
    prompt = instructions.format(json_data=profile_bytes.decode("utf-8"), data_description="")
    messages = [{"role": "system", "content": role}, {"role": "user", "content": prompt}]

    def source_check():
        if _read_source(bound["source_path"], request["source"]["sha256"])[1] != source_digest:
            raise PermissionError("selected source changed before model dispatch")
        private_copy = bound["output_path"] / "input-copy.csv"
        if _read_source(private_copy, source_digest)[1] != source_digest:
            raise PermissionError("private CSV copy changed before model dispatch")
        session = _path_without_symlinks(
            project_root, request["session_output_path"], "session_output_path", expect_directory=True
        )
        info = session.stat()
        if session != bound["output_path"] or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise PermissionError("authorized session directory changed before model dispatch")

    scoped_disclosure = ScopedDisclosure(
        auth, auth["model_alias"], messages, source_check, started_at=started_at
    )
    llm_metadata["role"] = role
    llm_metadata["scoped_authorization"] = scoped_disclosure

    from GUI.LLM_base.LlmBase import AbstractLlm

    llm = AbstractLlm(llm_metadata)
    candidate = llm.get_api_response(prompt, temperature=0, max_tokens=auth["max_output_tokens_total"])
    if not isinstance(candidate, str) or not candidate.strip():
        raise RuntimeError("provider returned an empty candidate")
    candidate_bytes = candidate.encode("utf-8")
    if len(candidate_bytes) > MAX_CANDIDATE_BYTES:
        raise RuntimeError("candidate Turtle exceeds 4 MiB")
    if hashlib.sha256(source_bytes).hexdigest() != source_digest:
        raise RuntimeError("private input copy source digest changed during profiling")
    if _read_source(bound["source_path"], request["source"]["sha256"])[1] != source_digest:
        raise RuntimeError("selected source changed during candidate generation")

    _write_private_file(bound["output_path"], "data-profile.json", profile_bytes)
    _write_private_file(bound["output_path"], "candidate.ttl", candidate_bytes)
    prompt_digest = hashlib.sha256(
        json.dumps(messages, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    result = {
        "contract_version": 1,
        "run_id": request["run_id"],
        "mode": "headless",
        "status": "candidate",
        "artifacts": [
            {"path": "data-profile.json", "media_type": "application/json",
             "sha256": profile_digest, "size_bytes": len(profile_bytes)},
            {"path": "candidate.ttl", "media_type": "text/turtle",
             "sha256": hashlib.sha256(candidate_bytes).hexdigest(), "size_bytes": len(candidate_bytes)},
        ],
        "provenance": {
            "source_id": request["source"]["source_id"],
            "source_sha256": source_digest,
            "approved_scope_ref": request["approved_scope_ref"],
            "approved_cq_ids": request["approved_cq_ids"],
            "model_alias": auth["model_alias"],
            "authorization_ref": auth["authorization_ref"],
            "model_request_count": scoped_disclosure.request_count,
            "profile_sha256": profile_digest,
            "prompt_sha256": prompt_digest,
            "resource_sha256": resource_digest,
        },
    }
    result_bytes = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(result_bytes) > MAX_RESULT_BYTES:
        raise RuntimeError("result.json exceeds 64 KiB")
    _write_private_file(bound["output_path"], "result.json", result_bytes)
    return result, result_bytes


def main(argv=None):
    parser = argparse.ArgumentParser(description="Generate an authorized bottom-up candidate from a bounded CSV profile.")
    parser.add_argument("--request", required=True, choices=("-",), help="read the closed JSON request from stdin")
    parser.add_argument("--output-dir", required=True, help="exact pre-authorized private session directory")
    arguments = parser.parse_args(argv)
    request_text = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(request_text) > MAX_REQUEST_BYTES:
        print("invalid request: stdin exceeds 65,536 bytes", file=sys.stderr)
        return 2
    try:
        result, result_bytes = run_request(request_text.decode("utf-8"), arguments.output_dir)
    except InvalidRequest as error:
        print(f"invalid request: {error}", file=sys.stderr)
        return 2
    except PermissionError as error:
        print(f"execution blocked: {error}", file=sys.stderr)
        return 2
    except Exception:
        print("headless candidate execution failed", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(result_bytes + b"\n")
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
