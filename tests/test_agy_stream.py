import json
import queue
import subprocess
from pathlib import Path

import pytest

from jellydub.models import JellyDubError
from jellydub.translate import AgyStreamTranslator, AgyTranslator, get_translator


class FakeOutput:
    def __init__(self):
        self.lines = queue.Queue()
        self.closed = False

    def __iter__(self):
        while (line := self.lines.get()) is not None:
            yield line

    def emit(self, value):
        self.lines.put(value if isinstance(value, str) else json.dumps(value) + '\n')

    def close(self):
        self.closed = True


class FakeInput:
    def __init__(self, process):
        self.process = process
        self.closed = False

    def write(self, value):
        event = json.loads(value)
        assert event['event'] == 'user'
        self.process.messages.append(event)
        self.process.respond(self.process, event)

    def flush(self):
        pass

    def close(self):
        self.closed = True


class FakeProcess:
    def __init__(self, respond):
        self.respond = respond
        self.stdout = FakeOutput()
        self.stdin = FakeInput(self)
        self.messages = []
        self.returncode = None
        self.terminated = self.killed = self.waited = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15
        self.stdout.lines.put(None)

    def kill(self):
        self.killed = True
        self.returncode = -9
        self.stdout.lines.put(None)

    def wait(self, timeout):
        self.waited = True
        return self.returncode


def success(process, event):
    prompt = json.loads(event['message']['content'].split('\n', 1)[1])
    process.stdout.emit({'event': 'init'})
    process.stdout.emit({'event': 'step_update', 'text': 'working'})
    process.stdout.emit({'event': 'result', 'result': {
        'status': 'SUCCESS', 'structured_output': {
            'lines': ['vi ' + cue['text'] for cue in prompt['cues']]}}})


@pytest.fixture
def processes(monkeypatch):
    created = []
    responders = []

    def popen(args, **kwargs):
        directory = Path(kwargs['cwd'])
        assert directory.is_dir() and list(directory.iterdir()) == []
        assert directory != Path.cwd()
        assert args[:5] == ['agy', '--input-format', 'stream-json', '--output-format', 'stream-json']
        assert '--sandbox' in args and '--disable-slash-commands' in args
        assert kwargs['stderr'] == subprocess.DEVNULL
        assert kwargs['stdin'] == kwargs['stdout'] == subprocess.PIPE
        assert kwargs['encoding'] == 'utf-8'
        schema = json.loads(args[args.index('--json-schema') + 1])
        assert schema['required'] == ['lines']
        assert schema['properties']['lines']['items'] == {'type': 'string'}
        assert schema['additionalProperties'] is False
        process = FakeProcess(responders.pop(0) if responders else success)
        created.append((process, directory))
        return process

    monkeypatch.setattr('jellydub.translate.subprocess.Popen', popen)
    return created, responders


def test_factory_reuse_and_cleanup(processes):
    created, _ = processes
    translator = get_translator('agy-stream')
    assert isinstance(translator, AgyStreamTranslator)
    assert translator.model_name == 'gemini-3.8-flash-low'
    assert translator.cache_id() != AgyTranslator(translator.model_name).cache_id()
    assert get_translator('agy-stream', 'custom').model_name == 'custom'
    assert translator.translate_batch(['Hello']) == ['vi Hello']
    assert translator.translate_batch(['World']) == ['vi World']
    assert len(created) == 1
    process, directory = created[0]
    assert len(process.messages) == 2 and directory.exists()
    translator.close()
    translator.close()
    assert process.terminated and process.waited
    assert process.stdin.closed and process.stdout.closed
    assert not directory.exists()


def test_history_rotation(processes):
    created, _ = processes
    translator = AgyStreamTranslator(max_turns=2)
    try:
        for _ in range(3):
            assert translator.translate_batch(['Hello']) == ['vi Hello']
        assert len(created) == 2
        assert created[0][0].terminated and not created[0][1].exists()
    finally:
        translator.close()


@pytest.mark.parametrize('failure', ['eof', 'malformed-json', 'error', 'invalid-count', 'invalid-structure', 'timeout'])
def test_failure_restarts_before_retry(processes, failure):
    created, responders = processes

    def fail(process, event):
        if failure == 'eof':
            process.stdout.lines.put(None)
        elif failure == 'malformed-json':
            process.stdout.emit('not json\n')
        elif failure == 'error':
            # Explicit status must win even when structured_output looks valid.
            process.stdout.emit({'event': 'result', 'result': {
                'status': 'ERROR', 'structured_output': {'lines': ['wrong']}}})
        elif failure == 'invalid-count':
            process.stdout.emit({'event': 'result', 'result': {
                'status': 'SUCCESS', 'structured_output': {'lines': []}}})
        elif failure == 'invalid-structure':
            process.stdout.emit({'event': 'result', 'result': []})

    responders.extend([fail, success])
    translator = AgyStreamTranslator(timeout=.02)
    try:
        assert translator.translate_batch(['Hello']) == ['vi Hello']
        assert len(created) == 2
        assert created[0][0].terminated and created[0][0].waited
        assert not created[0][1].exists()
    finally:
        translator.close()


@pytest.mark.parametrize('count', [1, 6])
def test_terminal_timeout_reaps_every_attempt(processes, count):
    created, responders = processes
    responders.extend([lambda process, event: None] * 3)
    translator = AgyStreamTranslator(timeout=.01)
    with pytest.raises(JellyDubError, match='after retries'):
        translator.translate_batch(['Hello'] * count)
    assert len(created) == 3
    assert all(process.terminated and process.waited and not directory.exists()
               for process, directory in created)
    assert translator._process is None


def test_invalid_line_count_still_bisects(processes):
    created, responders = processes

    def no_lines(process, event):
        process.stdout.emit({'event': 'result', 'result': {
            'status': 'SUCCESS', 'structured_output': {'lines': []}}})

    responders.extend([no_lines] * 3 + [success])
    translator = AgyStreamTranslator()
    try:
        assert translator.translate_batch(['Hello', 'World']) == ['vi Hello', 'vi World']
        assert len(created) == 4
        assert len(created[-1][0].messages) == 2
    finally:
        translator.close()


def test_missing_cli_cleans_directory(monkeypatch):
    directories = []

    def missing(args, **kwargs):
        directories.append(Path(kwargs['cwd']))
        raise FileNotFoundError()

    monkeypatch.setattr('jellydub.translate.subprocess.Popen', missing)
    translator = AgyStreamTranslator()
    with pytest.raises(JellyDubError, match='not installed'):
        translator.translate_batch(['Hello'])
    assert len(directories) == 1 and not directories[0].exists()


def test_close_kills_unresponsive_process(processes):
    created, _ = processes
    translator = AgyStreamTranslator()
    translator.translate_batch(['Hello'])
    process, directory = created[0]
    original_wait = process.wait

    def wait(timeout):
        if not process.killed:
            raise subprocess.TimeoutExpired('agy', timeout)
        return original_wait(timeout)

    process.wait = wait
    translator.close()
    assert process.killed and process.waited and not directory.exists()


@pytest.mark.parametrize('kwargs', [{'timeout': 0}, {'max_turns': 0}])
def test_invalid_limits(kwargs):
    with pytest.raises(ValueError):
        AgyStreamTranslator(**kwargs)
