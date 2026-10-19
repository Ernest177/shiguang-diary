"""Register new prompt versions; existing version hashes are immutable."""
import json

from app.prompts import prompt_metadata
from evals.run import DATA, read_json


def main():
    path = DATA / "prompt-registry.json"
    registry = read_json(path)
    for value in prompt_metadata().values():
        version, sha = value["version"], value["sha256"]
        if version in registry and registry[version] != sha:
            raise ValueError("旧版本不能覆写，请先提升提示词版本：" + version)
        registry[version] = sha
    path.write_text(json.dumps(registry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Prompt registry updated; review and commit it with the prompt change.")


if __name__ == "__main__":
    main()
