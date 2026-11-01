import pytest

from jellydub.models import Cue, JellyDubError
from jellydub.translate import OpusMTTranslator, get_translator, translate_cues


class FakeTranslator:
    def __init__(self, identity='fake'):
        self.calls = []
        self.identity = identity

    def cache_id(self):
        return self.identity

    def translate_batch(self, texts):
        self.calls.append(texts)
        return ['vi ' + text for text in texts]

    def close(self):
        pass


def test_cache_and_metadata(tmp_path):
    cues = [Cue(7, 1.001, 2.002, 'Hello.', 'TYSON'), Cue(9, 4, 5, 'Bye!')]
    fake = FakeTranslator()
    progress = []
    out = translate_cues(cues, fake, tmp_path, batch_size=1, on_progress=lambda a, b: progress.append((a, b)))
    assert len(fake.calls) == 2
    assert out[0] == Cue(7, 1.001, 2.002, 'vi Hello.', 'TYSON')
    assert cues[0].text == 'Hello.'
    assert progress[-1] == (2, 2)
    assert translate_cues(cues, fake, tmp_path) == out
    assert len(fake.calls) == 2
    other = FakeTranslator('different-model')
    translate_cues(cues, other, tmp_path)
    assert other.calls


def test_context_and_corrupt_cache(tmp_path):
    cues = [Cue(1, 0, 1, 'This is'), Cue(2, 1.1, 2, 'a sentence.')]
    fake = FakeTranslator()
    out = translate_cues(cues, fake, tmp_path)
    assert fake.calls == [['This is a sentence.']]
    assert ' '.join(c.text for c in out) == 'vi This is a sentence.'
    next(tmp_path.glob('*.json')).write_text('{broken')
    assert translate_cues(cues, fake, tmp_path) == out
    assert len(fake.calls) == 2


def test_resume_after_failure(tmp_path):
    cues = [Cue(i, i, i + .5, f'{i}.') for i in range(3)]
    class Interrupted(FakeTranslator):
        def translate_batch(self, texts):
            if self.calls:
                raise RuntimeError('interrupted')
            return super().translate_batch(texts)
    with pytest.raises(JellyDubError):
        translate_cues(cues, Interrupted(), tmp_path, batch_size=1)
    fake = FakeTranslator()
    translate_cues(cues, fake, tmp_path)
    assert fake.calls == [['1.', '2.']]


def test_validation_and_empty(tmp_path):
    fake = FakeTranslator()
    assert translate_cues([], fake, tmp_path) == []
    assert translate_cues([Cue(1, 0, 1, '')], fake, tmp_path)[0].text == ''
    assert fake.calls == []
    with pytest.raises(JellyDubError):
        translate_cues([], fake, tmp_path, batch_size=0)
    with pytest.raises(JellyDubError):
        get_translator('remote')
    assert isinstance(get_translator(), OpusMTTranslator)


def test_invalid_backend_output(tmp_path):
    fake = FakeTranslator()
    fake.translate_batch = lambda texts: []
    with pytest.raises(JellyDubError):
        translate_cues([Cue(1, 0, 1, 'Hello.')], fake, tmp_path)


@pytest.mark.slow
def test_real_model():
    translator = get_translator(device='cpu')
    try:
        result = translator.translate_batch(['Hello, how are you?'])
        assert len(result) == 1 and result[0].strip()
    finally:
        translator.close()


def test_short_context_translation_falls_back(tmp_path):
    fake = FakeTranslator()
    def translate(texts):
        fake.calls.append(texts)
        return ['Chào'] if len(texts) == 1 else ['Xin', 'chào']
    fake.translate_batch = translate
    out = translate_cues([Cue(1, 0, 1, 'Hello'), Cue(2, 1, 2, 'there.')], fake, tmp_path)
    assert [c.text for c in out] == ['Xin', 'chào']
    assert len(fake.calls) == 2


def test_agy_factory():
    from jellydub.translate import AgyTranslator, DEFAULT_MODEL
    assert get_translator(model=None).model_name == DEFAULT_MODEL
    assert isinstance(get_translator('agy'), AgyTranslator)
    assert get_translator('agy').model_name == 'gemini-3.8-flash-medium'
    assert get_translator('agy', model='custom').cache_id() != get_translator('agy').cache_id()
    assert 'v2' in get_translator().cache_id()


def test_agy_chunks_context_isolation_cache(monkeypatch, tmp_path):
    import json
    from pathlib import Path
    from types import SimpleNamespace
    from jellydub.translate import AgyTranslator
    calls = []
    def run(args, **kwargs):
        assert args[0] == 'agy'
        assert '--sandbox' in args
        assert '--dangerously-skip-permissions' not in args
        directory = Path(kwargs['cwd'])
        assert directory.is_dir() and list(directory.iterdir()) == []
        assert directory != Path.cwd()
        assert kwargs['timeout'] == 660
        assert args[-2:] == ['--print-timeout', '600s']
        schema = json.loads(args[args.index('--json-schema') + 1])
        assert schema['required'] == ['lines']
        payload = json.loads(args[2].split('\n', 1)[1])
        calls.append((payload, directory))
        return SimpleNamespace(returncode=0, stderr='', stdout=json.dumps({
            'structured_output': {'lines': ['vi ' + c['text'] for c in payload['cues']]}}))
    monkeypatch.setattr('jellydub.translate.subprocess.run', run)
    cues = [Cue(i, i * 2, i * 2 + 1.5, f'Line {i}', 'TYSON') for i in range(126)]
    translator = AgyTranslator()
    result = translate_cues(cues, translator, tmp_path)
    assert [len(c[0]['cues']) for c in calls] == [120, 6]
    assert calls[0][0]['cues'][0]['duration_seconds'] == 1.5
    assert calls[1][0]['context'] == [{'source': f'Line {i}', 'translated': f'vi Line {i}'} for i in range(115, 120)]
    assert all(not directory.exists() for _, directory in calls)
    assert result[0] == Cue(0, 0, 1.5, 'vi Line 0', 'TYSON')
    assert translate_cues(cues, translator, tmp_path) == result
    assert len(calls) == 2
    changed = list(cues)
    changed[-1] = Cue(125, 250, 253, 'Line 125', 'TYSON')
    translate_cues(changed, translator, tmp_path)
    assert len(calls) == 3


def test_agy_retries_then_splits(monkeypatch):
    import json
    from types import SimpleNamespace
    from jellydub.translate import AgyTranslator
    counts = []
    def run(args, **kwargs):
        payload = json.loads(args[2].split('\n', 1)[1])
        count = len(payload['cues'])
        counts.append(count)
        lines = [] if count > 1 else ['Chào']
        return SimpleNamespace(returncode=0, stderr='', stdout=json.dumps({'structured_output': {'lines': lines}}))
    monkeypatch.setattr('jellydub.translate.subprocess.run', run)
    assert AgyTranslator().translate_batch(['Hello', 'there']) == ['Chào', 'Chào']
    assert counts == [2, 2, 2, 1, 1]


@pytest.mark.parametrize('mode', ['missing', 'login', 'invalid', 'timeout', 'exit'])
def test_agy_errors(monkeypatch, mode):
    import subprocess
    from types import SimpleNamespace
    from jellydub.translate import AgyTranslator
    calls = []
    def run(*args, **kwargs):
        calls.append(1)
        if mode == 'missing':
            raise FileNotFoundError()
        if mode == 'timeout':
            raise subprocess.TimeoutExpired('agy', 660)
        return SimpleNamespace(returncode=1 if mode in ('login', 'exit') else 0,
                               stderr='not logged in' if mode == 'login' else '', stdout='bad json')
    monkeypatch.setattr('jellydub.translate.subprocess.run', run)
    with pytest.raises(JellyDubError, match={'missing': 'not installed', 'login': 'not logged in'}.get(mode, 'after retries')):
        AgyTranslator().translate_batch(['Hello'])
    assert len(calls) == (1 if mode in ('missing', 'login') else 3)


def test_optional_cue_method_preferred(tmp_path):
    class Backend(FakeTranslator):
        def translate_cues_batch(self, cues):
            assert cues[0].duration == 2
            return ['Chào']
        def translate_batch(self, texts):
            pytest.fail('should use cue method')
    assert translate_cues([Cue(1, 0, 2, 'Hi')], Backend(), tmp_path)[0].text == 'Chào'


def test_opus_generation_guards_and_retry(monkeypatch):
    import sys
    from contextlib import nullcontext
    from types import SimpleNamespace
    class Inputs(dict):
        def to(self, device):
            return self
    class Tokenizer:
        model_max_length = 512
        def __call__(self, *args, **kwargs):
            return Inputs(input_ids=SimpleNamespace(shape=(1, 10)))
        def batch_decode(self, output, **kwargs):
            return output
    calls = []
    class Model:
        device = 'cpu'
        def generate(self, **kwargs):
            calls.append(kwargs)
            return ['mảnh mảnh mảnh'] if len(calls) == 1 else ['Xin chào']
    monkeypatch.setitem(sys.modules, 'torch', SimpleNamespace(inference_mode=nullcontext))
    translator = OpusMTTranslator()
    translator._model, translator._tokenizer = Model(), Tokenizer()
    assert translator.translate_batch(['Hello']) == ['Xin chào']
    assert len(calls) == 2
    assert calls[0]['no_repeat_ngram_size'] == 3
    assert calls[0]['num_beams'] == 4
    assert calls[0]['repetition_penalty'] == 1.2
    assert calls[1]['repetition_penalty'] > calls[0]['repetition_penalty']
    assert calls[0]['max_new_tokens'] == 32
    translator._model.generate = lambda **kwargs: ['trái trái trái']
    assert translator.translate_batch(['Hello']) == ['trái']
    assert calls[0]['max_length'] is None


@pytest.mark.slow
def test_real_agy():
    translator = get_translator('agy')
    try:
        lines = translator.translate_cues_batch([Cue(1, 0, 3, 'We explore the universe together.')])
        assert len(lines) == 1 and lines[0].strip()
    finally:
        translator.close()


@pytest.mark.parametrize(('text', 'expected'), [
    ('trái trái tim', 'trái tim'),
    ('mảnh mảnh mảnh mảnh', 'mảnh'),
    ('trái tim trái tim trái tim', 'trái tim'),
    ('Trái trái tim.', 'Trái tim.'),
    ('Một trái tim, một trái tim.', 'Một trái tim, một trái tim.'),
    ('Xin chào!', 'Xin chào!'),
])
def test_collapse_repetitions(text, expected):
    from jellydub.translate import _collapse_repetitions
    assert _collapse_repetitions(text) == expected


def test_opus_bad_cue_keeps_movie_running(monkeypatch, tmp_path, caplog):
    import sys
    from contextlib import nullcontext
    from types import SimpleNamespace
    from jellydub.subtitles import parse_srt, write_srt

    class Inputs(dict):
        def to(self, device):
            return self
    class Tokenizer:
        model_max_length = 512
        def __call__(self, texts, **kwargs):
            return Inputs(input_ids=SimpleNamespace(shape=(len(texts), 10)), texts=texts)
        def batch_decode(self, output, **kwargs):
            return output
    calls = []
    class Model:
        device = 'cpu'
        def generate(self, **kwargs):
            calls.append(kwargs)
            assert kwargs['max_length'] is None
            return [('trái trái tim trái tim' if text == 'Line 407.' else
                     ('Bản dịch.' if kwargs['repetition_penalty'] == 1.2 else 'Changed on retry.'))
                    for text in kwargs['texts']]
    monkeypatch.setitem(sys.modules, 'torch', SimpleNamespace(inference_mode=nullcontext))
    translator = OpusMTTranslator()
    translator._model, translator._tokenizer = Model(), Tokenizer()
    cues = [Cue(i, i * 2, i * 2 + 1, f'Line {i}.') for i in range(1, 616)]
    progress = []
    result = translate_cues(cues, translator, tmp_path / 'cache', on_progress=lambda a, b: progress.append((a, b)))
    assert len(result) == 615
    assert result[406].text == 'trái tim'
    assert all(c.text == 'Bản dịch.' for i, c in enumerate(result) if i != 406)
    assert progress[-1] == (615, 615)
    assert 'cue index 407' in caplog.text and '.vi.srt' in caplog.text
    assert len([r for r in caplog.records if 'Collapsed' in r.message]) == 1
    output = tmp_path / 'movie.vi.srt'
    write_srt(result, output)
    assert parse_srt(output) == result
    count = len(calls)
    assert translate_cues(cues, translator, tmp_path / 'cache') == result
    assert len(calls) == count


@pytest.fixture
def gemini_env(monkeypatch):
    monkeypatch.setenv('GEMINI_API_KEY', 'unit-test-secret-key')
    monkeypatch.delenv('GOOGLE_API_KEY', raising=False)
    sleeps = []
    monkeypatch.setattr('jellydub.translate.time.sleep', sleeps.append)
    monkeypatch.setattr('jellydub.translate.random.uniform', lambda a, b: 0.25)
    return sleeps


def _gemini_response(lines=None, payload=None):
    import io
    import json
    if payload is None:
        payload = {'candidates': [{'finishReason': 'STOP', 'content': {'parts': [
            {'text': json.dumps({'lines': lines}, ensure_ascii=False)}]}}]}
    return io.BytesIO(json.dumps(payload).encode('utf-8'))


def test_gemini_request_cache_and_context(monkeypatch, tmp_path, gemini_env, caplog):
    import json
    from jellydub.translate import GeminiTranslator
    calls = []
    def urlopen(request, **kwargs):
        assert request.full_url == 'https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent'
        assert 'unit-test-secret-key' not in request.full_url
        assert request.get_header('X-goog-api-key') == 'unit-test-secret-key'
        assert request.method == 'POST' and kwargs['timeout'] == 660
        body = json.loads(request.data)
        config = body['generationConfig']
        assert config['responseMimeType'] == 'application/json'
        assert config['responseSchema']['required'] == ['lines']
        assert 'additionalProperties' not in config['responseSchema']
        assert config['temperature'] == .3
        prompt = body['contents'][0]['parts'][0]['text']
        data = json.loads(prompt.split('\n', 1)[1])
        calls.append(data)
        return _gemini_response(['vi ' + c['text'] for c in data['cues']])
    monkeypatch.setattr('jellydub.translate.urllib.request.urlopen', urlopen)
    translator = get_translator('gemini')
    assert isinstance(translator, GeminiTranslator)
    assert get_translator('gemini', model='custom').cache_id() != translator.cache_id()
    cues = [Cue(i, i * 2, i * 2 + 1.5, f'Line {i}', 'TYSON') for i in range(126)]
    result = translate_cues(cues, translator, tmp_path)
    assert [len(c['cues']) for c in calls] == [120, 6]
    assert calls[0]['cues'][0]['duration_seconds'] == 1.5
    assert calls[1]['context'] == [{'source': f'Line {i}', 'translated': f'vi Line {i}'} for i in range(115, 120)]
    assert result[0] == Cue(0, 0, 1.5, 'vi Line 0', 'TYSON')
    assert translate_cues(cues, translator, tmp_path) == result
    assert len(calls) == 2
    assert 'unit-test-secret-key' not in translator.cache_id() + caplog.text
    assert all('unit-test-secret-key' not in p.read_text() for p in tmp_path.glob('*.json'))
    assert gemini_env == []


@pytest.mark.parametrize('status', [400, 401, 403, 404])
def test_gemini_permanent_http_errors(monkeypatch, gemini_env, status, caplog):
    import io
    import traceback
    from urllib.error import HTTPError
    from jellydub.translate import GeminiTranslator
    calls = []
    def urlopen(*args, **kwargs):
        calls.append(1)
        raise HTTPError('https://example.invalid/unit-test-secret-key', status, 'unit-test-secret-key', {},
                        io.BytesIO(b'unit-test-secret-key'))
    monkeypatch.setattr('jellydub.translate.urllib.request.urlopen', urlopen)
    with pytest.raises(JellyDubError) as caught:
        GeminiTranslator().translate_batch(['Hello'])
    assert f'HTTP {status}' in str(caught.value)
    if status in (400, 401, 403):
        assert 'set GEMINI_API_KEY from https://aistudio.google.com/apikey' in str(caught.value)
    assert 'unit-test-secret-key' not in ''.join(traceback.format_exception(caught.value)) + caplog.text
    assert len(calls) == 1 and gemini_env == []


@pytest.mark.parametrize('mode', [429, 500, 503, 'timeout', 'urlerror'])
def test_gemini_transient_backoff(monkeypatch, gemini_env, mode):
    import io
    from urllib.error import HTTPError, URLError
    from jellydub.translate import GeminiTranslator
    calls = []
    def urlopen(*args, **kwargs):
        calls.append(1)
        if len(calls) < 3:
            if mode == 'timeout':
                raise TimeoutError('unit-test-secret-key')
            if mode == 'urlerror':
                raise URLError('unit-test-secret-key')
            raise HTTPError('https://example.invalid', mode, 'failure', {'Retry-After': '7'}, io.BytesIO())
        return _gemini_response(['Chào'])
    monkeypatch.setattr('jellydub.translate.urllib.request.urlopen', urlopen)
    assert GeminiTranslator().translate_batch(['Hello']) == ['Chào']
    assert len(calls) == 3
    assert gemini_env == ([1.25, 2.25] if isinstance(mode, str) else [7, 7])


@pytest.mark.parametrize('payload', [
    {'promptFeedback': {'blockReason': 'SAFETY'}},
    {'candidates': []},
    {'candidates': [{'finishReason': 'SAFETY', 'content': {'parts': []}}]},
    {'candidates': [{'content': {'parts': [{'text': 'invalid json'}]}}]},
])
def test_gemini_blocked_or_invalid_response_bisects(monkeypatch, gemini_env, payload):
    import json
    from jellydub.translate import GeminiTranslator
    counts = []
    def urlopen(request, **kwargs):
        data = json.loads(json.loads(request.data)['contents'][0]['parts'][0]['text'].split('\n', 1)[1])
        count = len(data['cues'])
        counts.append(count)
        return _gemini_response(payload=payload) if count > 1 else _gemini_response(['Chào'])
    monkeypatch.setattr('jellydub.translate.urllib.request.urlopen', urlopen)
    assert GeminiTranslator().translate_batch(['Hello', 'there']) == ['Chào', 'Chào']
    assert counts == [2, 2, 2, 1, 1]
    assert gemini_env == [1.25, 2.25]


def test_gemini_missing_key_and_fallback(monkeypatch, gemini_env):
    from jellydub.translate import GeminiTranslator
    monkeypatch.delenv('GEMINI_API_KEY')
    calls = []
    def urlopen(request, **kwargs):
        calls.append(request.get_header('X-goog-api-key'))
        return _gemini_response(['Chào'])
    monkeypatch.setattr('jellydub.translate.urllib.request.urlopen', urlopen)
    with pytest.raises(JellyDubError, match='set GEMINI_API_KEY'):
        GeminiTranslator().translate_batch(['Hello'])
    assert calls == []
    monkeypatch.setenv('GOOGLE_API_KEY', 'fallback-test-key')
    assert GeminiTranslator().translate_batch(['Hello']) == ['Chào']
    assert calls == ['fallback-test-key']
    monkeypatch.setenv('GEMINI_API_KEY', 'preferred-test-key')
    GeminiTranslator().translate_batch(['Hello'])
    assert calls[-1] == 'preferred-test-key'


def test_gemini_terminal_error_does_not_leak(monkeypatch, gemini_env, caplog):
    import traceback
    from jellydub.translate import GeminiTranslator
    def urlopen(*args, **kwargs):
        raise TimeoutError('unit-test-secret-key')
    monkeypatch.setattr('jellydub.translate.urllib.request.urlopen', urlopen)
    with pytest.raises(JellyDubError, match='after retries') as caught:
        GeminiTranslator().translate_batch(['Hello'])
    assert 'unit-test-secret-key' not in ''.join(traceback.format_exception(caught.value)) + caplog.text
    assert gemini_env == [1.25, 2.25]


def test_retry_after_date():
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime
    from jellydub.translate import _retry_after_seconds
    future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=30))
    assert 28 <= _retry_after_seconds(future) <= 30
    assert _retry_after_seconds('bad value') == 0
    assert _retry_after_seconds('-1') == 0


@pytest.mark.slow
def test_real_gemini():
    import os
    if not (os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY')):
        pytest.skip('Gemini API key not configured')
    translator = get_translator('gemini')
    try:
        result = translator.translate_cues_batch([Cue(1, 0, 3, 'We explore the universe together.')])
        assert len(result) == 1 and result[0].strip()
    finally:
        translator.close()


@pytest.mark.parametrize('mode', ['line-count', 'non-string', 'empty-line', 'malformed-body'])
def test_gemini_invalid_output_retries(monkeypatch, gemini_env, mode):
    import io
    from jellydub.translate import GeminiTranslator
    calls = []
    def urlopen(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            if mode == 'malformed-body':
                return io.BytesIO(b'not-json unit-test-secret-key')
            return _gemini_response({'line-count': [], 'non-string': [42], 'empty-line': ['']}[mode])
        return _gemini_response(['Chào'])
    monkeypatch.setattr('jellydub.translate.urllib.request.urlopen', urlopen)
    assert GeminiTranslator().translate_batch(['Hello']) == ['Chào']
    assert len(calls) == 2 and gemini_env == [1.25]
