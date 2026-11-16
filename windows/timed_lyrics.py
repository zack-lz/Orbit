"""Accurate lyric timing: matched Apple cache first; opt-in LRCLIB enrichment."""
import asyncio
import difflib
import json
import math
import os
from pathlib import Path
import re
import time
import unicodedata
import xml.etree.ElementTree as ET

import aiohttp


def clock(value):
    text = str(value).strip()
    if text.endswith('ms'):
        result = float(text[:-2]) / 1000
    else:
        if text.endswith('s'):
            text = text[:-1]
        parts = text.split(':')
        result = sum(float(part) * 60 ** index for index, part in enumerate(reversed(parts)))
    if not 0 <= result <= 86400:
        raise ValueError('Invalid lyric time')
    return result


def normal(text):
    return ''.join(c for c in unicodedata.normalize('NFKC', str(text)).casefold() if c.isalnum())


def finish(lines, duration, source):
    lines = sorted(lines[:2000], key=lambda line: line['start'])
    remaining = 8000
    for index, line in enumerate(lines):
        next_start = lines[index + 1]['start'] if index + 1 < len(lines) else duration
        line['end'] = max(line['start'], line.get('end') or next_start or line['start'] + 4)
        words = line.get('words', [])[:min(2000,remaining)]
        if words and normal(''.join(word['text'] for word in words)) != normal(line['text']):
            words = []
        remaining -= len(words)
        for wi, word in enumerate(words):
            word['end'] = max(word['start'], word.get('end') or (words[wi+1]['start'] if wi+1 < len(words) else line['end']))
        line['words'] = words
    mode = 'word' if any(line['words'] for line in lines) else 'line'
    return {'available':bool(lines), 'lines':[line['text'] for line in lines], 'timed_lines':lines,
            'mode':mode, 'synced':bool(lines), 'duration':duration, 'source':source}


def parse_ttml(raw):
    if len(raw) > 1_000_000 or '<!DOCTYPE' in raw.upper() or '<!ENTITY' in raw.upper():
        raise ValueError('Unsafe or oversized lyric XML')
    root = ET.fromstring(raw)
    body = next((e for e in root.iter() if e.tag.split('}')[-1] == 'body'), None)
    if body is None:
        raise ValueError('Missing lyric body')
    duration = clock(body.attrib.get('dur', '0'))
    lines = []
    for paragraph in body.iter():
        if paragraph.tag.split('}')[-1] != 'p' or 'begin' not in paragraph.attrib:
            continue
        start = clock(paragraph.attrib['begin'])
        end = clock(paragraph.attrib['end']) if 'end' in paragraph.attrib else None
        text = ''.join(paragraph.itertext()).strip()
        if not text:
            continue
        words = []
        for span in paragraph.iter():
            if span.tag.split('}')[-1] != 'span' or 'begin' not in span.attrib:
                continue
            if any(child is not span and child.tag.split('}')[-1] == 'span' and 'begin' in child.attrib for child in span.iter()):
                continue
            word_text = ''.join(span.itertext())
            if span.tail and '\n' not in span.tail:
                word_text += span.tail
            if word_text.strip():
                ws = clock(span.attrib['begin'])
                we = clock(span.attrib['end']) if 'end' in span.attrib else None
                if ws < start - 1:
                    ws += start
                    if we is not None:
                        we += start
                words.append({'text':word_text, 'start':ws, 'end':we})
        lines.append({'text':text, 'start':start, 'end':end, 'words':words})
    return finish(lines, duration, 'apple_local')


def parse_lrc(raw, duration):
    lines = []
    for row in raw.splitlines():
        stamps = re.findall(r'\[(\d+:\d+(?:\.\d+)?)\]', row)
        text = re.sub(r'\[[^]]*\]', '', row).strip()
        if text:
            for stamp in stamps:
                lines.append({'text':text, 'start':clock(stamp), 'end':None, 'words':[]})
    return finish(lines, duration, 'lrclib')


def parse_lyricsfile(raw, duration):
    if len(raw) > 500_000:
        raise ValueError('Oversized lyricsfile')
    import yaml
    try:
        data = yaml.safe_load(raw)
    except (yaml.YAMLError,RecursionError) as error:
        raise ValueError('Invalid lyric YAML') from error
    if not isinstance(data, dict) or str(data.get('version')) != '1.0':
        raise ValueError('Unsupported lyricsfile version')
    lines = []
    for row in data.get('lines', [])[:2000]:
        if not isinstance(row, dict) or not isinstance(row.get('text'), str):
            continue
        start = float(row['start_ms']) / 1000
        if not math.isfinite(start) or not 0 <= start <= duration + 10:
            continue
        words = []
        for word in row.get('words', [])[:2000]:
            if isinstance(word, dict) and isinstance(word.get('text'), str):
                ws = float(word['start_ms']) / 1000
                we = float(word['end_ms']) / 1000 if word.get('end_ms') is not None else None
                if math.isfinite(ws) and (we is None or math.isfinite(we)) and 0 <= ws <= duration + 10:
                    words.append({'text':word['text'], 'start':ws, 'end':we})
        end = clock(str(row['end_ms'])+'ms') if row.get('end_ms') is not None else None
        lines.append({'text':row['text'], 'start':start, 'end':end, 'words':words})
    return finish(lines, duration, 'lrclib')


class LyricService:
    def __init__(self):
        self.parsed = {}
        self.local_results = {}
        self.online_tasks = {}
        self.lock = asyncio.Lock()
        self.backoff_until = 0
        self.first_seen = {}

    def find_local(self, state, native):
        anchor = normal(''.join(native.get('lines', [])))
        if len(anchor) < 12:
            return None
        base = Path(os.environ.get('LOCALAPPDATA', '')) / 'Packages'
        files = []
        for root in base.glob('AppleInc.AppleMusicWin_*/AC/INetCache'):
            for pattern in ('syllable-lyrics*.json', 'ttmlLyrics*.json'):
                files.extend(root.rglob(pattern))
        indexed = []
        for file in files:
            try:
                indexed.append((file.stat().st_mtime,file))
            except OSError:
                pass
        files = [file for _,file in sorted(indexed,key=lambda item:item[0],reverse=True)[:200]]
        best, score = None, .74
        for file in files:
            try:
                stat = file.stat()
                if stat.st_size > 1_000_000:
                    continue
                signature = (str(file), stat.st_mtime_ns)
                if signature not in self.parsed:
                    data = json.loads(file.read_text(encoding='utf-8-sig'))
                    documents = []
                    if not isinstance(data,dict):
                        continue
                    for record in data.get('data', []):
                        if not isinstance(record,dict):
                            continue
                        attributes = record.get('attributes', {})
                        if not isinstance(attributes,dict):
                            continue
                        raw = attributes.get('ttmlLocalizations') or attributes.get('ttml')
                        if isinstance(raw, str) and raw.lstrip().startswith('<'):
                            documents.append(parse_ttml(raw))
                    self.parsed[signature] = documents
                    if len(self.parsed) > 250:
                        self.parsed.pop(next(iter(self.parsed)))
                for doc in self.parsed[signature]:
                    if abs(doc['duration'] - state.get('duration', 0)) > 3.25:
                        continue
                    ratio = difflib.SequenceMatcher(None, anchor, normal(''.join(doc['lines'])), autojunk=False).ratio()
                    if ratio > score or (best and ratio == score and doc['mode'] == 'word'):
                        best, score = doc, ratio
            except (OSError,ValueError,TypeError,KeyError,ET.ParseError):
                continue
        return dict(best) if best else None

    async def online(self, state):
        if time.monotonic() < self.backoff_until or not 1 <= state.get('duration', 0) <= 3600:
            return None
        params = {'track_name':state['title'], 'artist_name':state['artist'], 'duration':round(state['duration'])}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8), headers={'User-Agent':'AppleMusicRemote/0.1 (opt-in lyrics)'}, trust_env=False) as session:
                async with session.get('https://lrclib.net/api/get', params=params, allow_redirects=False) as response:
                    if response.status in (429,503):
                        retry = response.headers.get('Retry-After', '60')
                        self.backoff_until = time.monotonic() + min(600, max(30, int(retry) if retry.isdigit() else 60))
                        return None
                    if response.status != 200:
                        return None
                    raw = await response.content.read(1_000_001)
                    if len(raw) > 1_000_000:
                        return None
                    data = json.loads(raw)
            if not isinstance(data,dict):
                return None
            duration = float(data.get('duration', 0))
            if not math.isfinite(duration) or normal(data.get('trackName', '')) != normal(state['title']) or normal(data.get('artistName', '')) != normal(state['artist']) or abs(duration - state['duration']) > 3.25:
                return None
            if data.get('hasWordSync') and data.get('lyricsfile'):
                try:
                    doc = parse_lyricsfile(data['lyricsfile'], state['duration'])
                    if doc['available']:
                        return doc
                except (ImportError,ValueError,TypeError,KeyError):
                    pass
            if data.get('syncedLyrics'):
                return parse_lrc(data['syncedLyrics'], state['duration'])
        except (aiohttp.ClientError,asyncio.TimeoutError,ValueError,TypeError):
            pass
        return None

    async def resolve(self, state, native, online=False, force=False):
        key = tuple(state.get(k, '') for k in ('title','artist','album','duration'))
        async with self.lock:
            if force:
                self.local_results.pop(key, None)
                old = self.online_tasks.get(key)
                if old and old.done():
                    self.online_tasks.pop(key, None)
            self.first_seen.setdefault(key, time.monotonic())
            cached = self.local_results.get(key)
            if cached is None or ((not cached[1] or cached[1]['mode'] != 'word') and time.monotonic() - cached[0] > 2 and time.monotonic()-self.first_seen[key]<15):
                local = await asyncio.to_thread(self.find_local, state, native)
                cached = self.local_results[key] = (time.monotonic(), local)
                if len(self.local_results)>64:
                    oldest=next(iter(self.local_results))
                    self.local_results.pop(oldest,None)
                    self.first_seen.pop(oldest,None)
                    old=self.online_tasks.pop(oldest,None)
                    if old and not old.done():
                        old.cancel()
            local = cached[1]
            if online and (not local or local['mode'] != 'word') and key not in self.online_tasks:
                self.online_tasks[key] = asyncio.create_task(self.online(dict(state)))
            task = self.online_tasks.get(key) if online else None
        remote = task.result() if task and task.done() and not task.cancelled() and task.exception() is None else None
        doc = local
        if remote and (not doc or remote['mode'] == 'word' and doc['mode'] != 'word'):
            doc = remote
        if not doc:
            doc = {**native, 'mode':'plain', 'synced':False, 'timed_lines':[]}
        local_pending = (not local or local['mode'] != 'word') and time.monotonic()-self.first_seen.get(key,0)<12
        return {**doc, 'upgrade_pending':bool(task and not task.done() or local_pending)}

    async def close(self):
        tasks = list(self.online_tasks.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
