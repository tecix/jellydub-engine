from pathlib import Path

import pytest
from typer.testing import CliRunner

from jellydub.cli import app
from jellydub.config import load_config
from jellydub.models import JellyDubError

runner = CliRunner()


def test_dub_options(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr('jellydub.pipeline.dub', lambda *a, **kw: calls.append((a, kw)))
    result = runner.invoke(app, ['dub', 'movie[1].mkv', '--srt', 'vi.srt', '--source-lang', 'vi',
                                 '--tts', 'vieneu', '--reference', 'actor=voice.wav', '--mkv',
                                 '--mix', 'replace', '--fallback-voiceover', '--audio-stream', '2',
                                 '--out-dir', str(tmp_path)])
    assert result.exit_code == 0, result.output
    config = calls[0][0][2]
    assert config.source_lang == 'vi' and config.mkv and config.audio_stream == 2
    assert config.reference == ['actor=voice.wav'] and config.out_dir == tmp_path
    assert config.fallback_voiceover


def test_config_override_and_false_flag(monkeypatch, tmp_path):
    path = tmp_path / 'settings.toml'
    path.write_text('[jellydub]\nsource_lang="vi"\nmkv=true\nrate="+8%"\n')
    seen = []
    monkeypatch.setattr('jellydub.pipeline.dub', lambda v, s, c, **kw: seen.append(c))
    result = runner.invoke(app, ['dub', 'x.mkv', '--srt', 'x.srt', '--config', str(path), '--no-mkv'])
    assert result.exit_code == 0, result.output
    assert seen[0].source_lang == 'vi' and not seen[0].mkv and seen[0].rate == '+8%'


@pytest.mark.parametrize('args', [[], ['dub', 'x'], ['dub', 'x', '--srt', 'x', '--tts', 'bad']])
def test_invalid_cli(args):
    assert runner.invoke(app, args).exit_code != 0


def test_errors_are_readable(monkeypatch):
    def fail(*args, **kwargs):
        raise JellyDubError('try again')
    monkeypatch.setattr('jellydub.pipeline.dub', fail)
    result = runner.invoke(app, ['dub', 'x', '--srt', 'x'])
    assert result.exit_code == 1 and 'Error: try again' in result.output


def test_translate_command(monkeypatch):
    seen = []
    monkeypatch.setattr('jellydub.pipeline.translate', lambda v, s, c, **kw: seen.append(c) or Path('movie.vi.srt'))
    result = runner.invoke(app, ['translate', 'movie.mkv', '--srt', 'en.srt'])
    assert result.exit_code == 0 and 'movie.vi.srt' in result.output
    assert seen[0].source_lang == 'auto'


def test_doctor(monkeypatch):
    monkeypatch.setattr('jellydub.cli.shutil.which', lambda name: '/usr/bin/' + name)
    monkeypatch.setattr('jellydub.cli.importlib.util.find_spec', lambda name: object())
    result = runner.invoke(app, ['doctor'])
    assert result.exit_code == 0 and 'translate: installed' in result.output


@pytest.mark.parametrize('text', ['[jellydub]\nunknown=1', '[jellydub]\naudio_stream=-1',
                                   '[jellydub]\nmkv="yes"', '[jellydub]\nreference="bad"',
                                   '[jellydub]\ncache_dir=42', '[jellydub]\nrate=1'])
def test_invalid_config(tmp_path, text):
    path = tmp_path / 'bad.toml'
    path.write_text(text)
    with pytest.raises(JellyDubError):
        load_config(path)


@pytest.mark.parametrize('configured,override,expected', [
    (None, None, 4), (2, None, 2), (2, 3, 3), (None, 1, 1),
])
def test_tts_workers_cli_and_config(monkeypatch, tmp_path, configured, override, expected):
    seen = []
    monkeypatch.setattr('jellydub.pipeline.dub', lambda v, s, c, **kw: seen.append(c))
    args = ['dub', 'movie.mkv', '--srt', 'movie.vi.srt']
    if configured is not None:
        config = tmp_path / 'settings.toml'
        config.write_text(f'[jellydub]\ntts_workers={configured}\n')
        args += ['--config', str(config)]
    if override is not None:
        args += ['--tts-workers', str(override)]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert seen[0].tts_workers == expected


@pytest.mark.parametrize('value', ['0', '-1', '1.5', 'true', '"4"'])
def test_tts_workers_invalid_config(tmp_path, value):
    config = tmp_path / 'settings.toml'
    config.write_text(f'[jellydub]\ntts_workers={value}\n')
    with pytest.raises(JellyDubError, match='tts_workers must be a positive integer'):
        load_config(config)


@pytest.mark.parametrize('value', ['0', '-1', '1.5', 'many'])
def test_tts_workers_invalid_cli(monkeypatch, value):
    def forbidden(*args, **kwargs):
        pytest.fail('invalid workers must be rejected before the pipeline runs')
    monkeypatch.setattr('jellydub.pipeline.dub', forbidden)
    result = runner.invoke(app, ['dub', 'movie.mkv', '--srt', 'movie.vi.srt', '--tts-workers', value])
    assert result.exit_code == 2, result.output


@pytest.mark.parametrize('command', ['translate', 'dub'])
@pytest.mark.parametrize('configured,override,expected', [
    (None, None, ('opus-mt', None)),
    (('agy', 'configured-model'), None, ('agy', 'configured-model')),
    (None, ('agy', 'cli-model'), ('agy', 'cli-model')),
    (None, ('agy-stream', 'gemini-3.8-flash-low'), ('agy-stream', 'gemini-3.8-flash-low')),
    (('agy', 'configured-model'), ('opus-mt', 'cli-model'), ('opus-mt', 'cli-model')),
])
def test_translator_cli_config_precedence(monkeypatch, tmp_path, command, configured, override, expected):
    seen = []
    def stage(video, srt, config, **kwargs):
        seen.append((config.translator, config.translator_model))
        return Path('movie.vi.srt')
    monkeypatch.setattr(f'jellydub.pipeline.{command}', stage)
    args = [command, 'movie.mkv', '--srt', 'movie.en.srt']
    if configured:
        config = tmp_path / 'translator.toml'
        config.write_text(f'[jellydub]\ntranslator="{configured[0]}"\ntranslator_model="{configured[1]}"\n')
        args += ['--config', str(config)]
    if override:
        args += ['--translator', override[0], '--translator-model', override[1]]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert seen == [expected]


@pytest.mark.parametrize('command', ['translate', 'dub'])
def test_invalid_translator_rejected_before_pipeline(monkeypatch, command):
    def forbidden(*args, **kwargs):
        pytest.fail('invalid translator must not reach pipeline')
    monkeypatch.setattr(f'jellydub.pipeline.{command}', forbidden)
    result = runner.invoke(app, [command, 'movie.mkv', '--srt', 'movie.en.srt', '--translator', 'unknown'])
    assert result.exit_code == 1
    assert 'Invalid translator' in result.output


@pytest.mark.parametrize('value', ['1', 'false', '""', '"   "'])
def test_invalid_translator_model(tmp_path, value):
    config = tmp_path / 'translator.toml'
    config.write_text(f'[jellydub]\ntranslator_model={value}\n')
    with pytest.raises(JellyDubError, match='translator_model must be a nonempty string'):
        load_config(config)


@pytest.mark.parametrize('command', ['translate', 'dub'])
@pytest.mark.parametrize('use_cli', [False, True])
def test_gemini_options(monkeypatch, tmp_path, command, use_cli):
    seen = []
    def stage(video, srt, config, **kwargs):
        seen.append((config.translator, config.translator_model))
        return Path('movie.vi.srt')
    monkeypatch.setattr(f'jellydub.pipeline.{command}', stage)
    args = [command, 'movie.mkv', '--srt', 'movie.en.srt']
    config = tmp_path / 'gemini.toml'
    if use_cli:
        config.write_text('[jellydub]\ntranslator="agy"\ntranslator_model="old-model"\n')
        args += ['--translator', 'gemini', '--translator-model', 'test-model']
    else:
        config.write_text('[jellydub]\ntranslator="gemini"\ntranslator_model="test-model"\n')
    args += ['--config', str(config)]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert seen == [('gemini', 'test-model')]


def test_dub_optional_srt_and_subtitle_options(monkeypatch):
    seen = []
    monkeypatch.setattr('jellydub.pipeline.dub', lambda v, s, c, **kw: seen.append((s, c)))
    result = runner.invoke(app, ['dub', 'movie.mkv', '--subs', 'local', '--no-download',
                                 '--providers', 'opensubtitlescom,podnapisi', '--providers', 'gestdown'])
    assert result.exit_code == 0, result.output
    srt, config = seen[0]
    assert srt is None and config.subs == 'local' and not config.download
    assert config.providers == ['opensubtitlescom', 'podnapisi', 'gestdown']


def test_subs_command(monkeypatch, tmp_path):
    seen = []
    def stage(video, config, **kwargs):
        seen.append((video, config, kwargs))
        return tmp_path / 'movie.vi.srt'
    monkeypatch.setattr('jellydub.pipeline.subs', stage)
    result = runner.invoke(app, ['subs', 'movie.mkv', '--no-download', '--force', '--out-dir', str(tmp_path),
                                 '--providers', 'opensubtitlescom', '--lang', 'vi'])
    assert result.exit_code == 0, result.output
    _, config, options = seen[0]
    assert config.out_dir == tmp_path and not config.download
    assert options['lang'] == 'vi' and options['force']
    assert 'movie.vi.srt' in result.output


def test_subs_config_and_cli_override(monkeypatch, tmp_path):
    config_path = tmp_path / 'subs.toml'
    config_path.write_text('[jellydub]\ndownload=false\nproviders=["gestdown"]\n')
    seen = []
    monkeypatch.setattr('jellydub.pipeline.subs', lambda v, c, **kw: seen.append(c) or Path('movie.vi.srt'))
    result = runner.invoke(app, ['subs', 'movie.mkv', '--config', str(config_path), '--download'])
    assert result.exit_code == 0, result.output
    assert seen[0].download and seen[0].providers == ['gestdown']


@pytest.mark.parametrize('text', ['subs="bad"', 'download="yes"', 'providers="name"',
                                   'providers=[]', 'subtitle_credentials_env={password="not an env name"}',
                                   'subtitle_credentials_env={token="ENV_NAME"}'])
def test_invalid_subtitle_sourcing_config(tmp_path, text):
    path = tmp_path / 'subs.toml'
    path.write_text('[jellydub]\n' + text)
    with pytest.raises(JellyDubError):
        load_config(path)


@pytest.mark.parametrize('command', ['dub', 'subs'])
def test_search_online_flag_and_config_override(monkeypatch, tmp_path, command):
    seen = []
    def stage(*args, **kwargs):
        seen.append(args[2] if command == 'dub' else args[1])
        return Path('movie.vi.srt')
    monkeypatch.setattr(f'jellydub.pipeline.{command}', stage)
    config = tmp_path / 'online.toml'
    config.write_text('[jellydub]\nsearch_online=false\n')
    result = runner.invoke(app, [command, 'movie.mkv', '--config', str(config), '--search-online'])
    assert result.exit_code == 0, result.output
    assert seen[0].search_online
    config.write_text('[jellydub]\nsearch_online=true\n')
    result = runner.invoke(app, [command, 'movie.mkv', '--config', str(config), '--no-search-online'])
    assert result.exit_code == 0, result.output
    assert not seen[1].search_online
