from unittest.mock import Mock

from src import voice


def test_speech_text_removes_markdown_metadata_and_summarizes_tables():
    reply = """# Summary

**Done** — the task is complete.

| Name | Status |
| --- | --- |
| Build | Ready |
| Tests | Passed |
| Deploy | Pending |
| Extra | Hidden |

```python
print('internal')
```

![generated image](workspace/generated_images/result.png)
via openrouter:nex-agi/nex-n2.5-pro:free
"""

    spoken = voice.speech_text(reply)

    assert "Summary Done — the task is complete." in spoken
    assert "Table with columns: Name, Status." in spoken
    assert "Build; Ready." in spoken
    assert "The table contains 1 additional rows in the chat." in spoken
    assert "I included code in the chat." in spoken
    assert "generated_images" not in spoken
    assert "via openrouter" not in spoken
    assert "|" not in spoken
    assert "**" not in spoken


def test_stop_speaking_stops_current_engine(monkeypatch):
    engine = Mock()
    monkeypatch.setattr(voice, "_active_engine", engine)

    voice.stop_speaking()

    engine.stop.assert_called_once()


def test_speech_text_skips_internal_paths_and_keeps_link_label():
    spoken = voice.speech_text(
        "Read the [project plan](https://example.test/plan).\n"
        "workspace/generated_images/result.png\n"
        "- First item"
    )

    assert spoken == "Read the project plan. First item"
