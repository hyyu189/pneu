from __future__ import annotations

import resource

from test_grok_adapter import _FakeClient, _instance


def test_grok_adapter_mini_soak_reuses_one_session_for_25_wakes(tmp_path):
    client = _FakeClient()
    instance = _instance(tmp_path, lambda _command, **_kwargs: client)
    instance._mail_generation = lambda: ()
    before = resource.getrusage(resource.RUSAGE_SELF)

    for index in range(25):
        instance.run_generation((f"message-{index}.md",), prompt_timeout=1, drain_timeout=1)

    after = resource.getrusage(resource.RUSAGE_SELF)
    assert len(client.prompts) == 25
    assert after.ru_maxrss - before.ru_maxrss < 8 * 1024 * 1024
