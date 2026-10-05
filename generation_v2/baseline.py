"""Offline source manifest and call-site inventory for the 6.1 baseline."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
import zipfile


CALLS = {"text_chat", "text_chat_stream", "_plugin_llm_text_chat",
         "_call_memory_generation_llm", "single_text_chat", "_call_llm_compress",
         "_generate_evidence_first_diaries", "_extract_episode_blueprints_resilient"}


class Inventory(ast.NodeVisitor):
    def __init__(self):
        self.scope = []
        self.calls = []

    def visit_FunctionDef(self, node):
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node):
        name = getattr(node.func, "attr", getattr(node.func, "id", ""))
        if name in CALLS:
            self.calls.append({"owner": ".".join(self.scope), "callee": name,
                               "line": node.lineno,
                               "keywords": sorted(k.arg for k in node.keywords if k.arg)})
        self.generic_visit(node)


def build(root: Path, base_zip: Path) -> dict:
    with zipfile.ZipFile(base_zip) as archive:
        before = {n.split('/', 1)[1]: hashlib.sha256(archive.read(n)).hexdigest()
                  for n in archive.namelist() if '/' in n and not n.endswith('/')}
    after = {}
    calls = []
    for path in sorted(root.rglob('*')):
        if not path.is_file() or '__pycache__' in path.parts:
            continue
        relative = path.relative_to(root).as_posix()
        after[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        if path.suffix == '.py' and not relative.startswith(('tests/', 'scripts/', 'generation_v2/')):
            tree = ast.parse(path.read_text(encoding='utf-8-sig'), filename=relative)
            visitor = Inventory()
            visitor.visit(tree)
            calls.extend({"file": relative, **item} for item in visitor.calls)
    return {
        "baseline_sha256": hashlib.sha256(base_zip.read_bytes()).hexdigest(),
        "baseline_file_count": len(before), "candidate_file_count": len(after),
        "removed": sorted(before.keys() - after.keys()),
        "added": sorted(after.keys() - before.keys()),
        "modified": sorted(k for k in before.keys() & after.keys() if before[k] != after[k]),
        "files": after, "call_sites": calls,
        "limitations": ["Static inventory is not a dynamic execution graph.",
                        "Legacy call paths are retained and not certified as repaired."],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = build(args.root, args.baseline)
    with args.output.open('x', encoding='utf-8') as out:
        json.dump(result, out, ensure_ascii=False, indent=2)
    print(json.dumps({k: result[k] for k in ('removed','added','modified')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
