# -*- coding: utf-8 -*-
"""
Словарик — всплывающий словарь-помощник для Windows.

Маленькое окно поверх всех окон: по горячей клавише (по умолчанию Ctrl+Space)
появляется в любом приложении, слово ищется по Enter.
Русские слова — русский Викисловарь (ru.wiktionary.org),
английские — dictionaryapi.dev (запасной источник — en.wiktionary.org),
опечатки — Яндекс.Спеллер.

Запуск:             python slovarik.py
Без окна консоли:   pythonw slovarik.py   (или «Запуск.bat»)
Проверка в консоли: python slovarik.py --cli слово

Зависимости: только стандартная библиотека + пакет keyboard (глобальный хоткей).
"""

import html
import json
import os
import queue
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor

APP_NAME = 'Словарик'
APP_VERSION = '1.1'
CONFIG_FILE = 'slovarik.json'
CACHE_FILE = 'slovarik_cache.json'
LOG_FILE = 'slovarik.log'
USER_AGENT = 'Slovarik/1.0 (personal dictionary popup; Python urllib)'
YA_SPELLER = 'https://speller.yandex.net/services/spellservice.json/checkText'

DEFAULT_CONFIG = {
    'hotkey': 'ctrl+space',      # глобальная горячая клавиша
    'suppress_hotkey': False,    # True — «съедать» нажатие, чтобы оно не попадало в активное приложение
    'opacity': 0.98,             # прозрачность окна (0.3–1.0)
    'width': 480,                # размер окна в логических пикселях (до масштабирования DPI)
    'height': 410,
    'top_ratio': 0.16,           # отступ окна сверху (доля высоты экрана)
    'timeout': 10,               # таймаут запросов к API, сек
    'retries': 2,                # повторы при сетевых сбоях
    'max_defs': 5,               # сколько значений показывать на одну часть речи
}

tk = None  # модуль tkinter подключается в run_app(), чтобы --cli работал и без графики


# ---------------------------------------------------------------------------
#  Сеть
# ---------------------------------------------------------------------------

class RateLimited(Exception):
    """Сервис ответил 429 Too Many Requests (лимит частоты запросов)."""

    def __init__(self, wait=None, service=''):
        super().__init__('rate limited: %s' % service)
        self.wait = wait
        self.service = service


NET_EXCEPTIONS = (urllib.error.URLError, TimeoutError, ConnectionError, OSError,
                  json.JSONDecodeError, UnicodeDecodeError)


def http_get_json(url, params=None, timeout=10, retries=2, max_rate_wait=3.0):
    """GET-запрос с повторами; ответ — разобранный JSON (dict или list).

    При 429 и небольшим Retry-After ждём и повторяем, иначе — исключение RateLimited.
    """
    if params:
        url = url + ('&' if '?' in url else '?') + urllib.parse.urlencode(params)
    last_error = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                url, headers={'User-Agent': USER_AGENT, 'Accept': 'application/json'})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode('utf-8', 'replace')
            return json.loads(raw)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise  # «не найдено» — повторять бессмысленно
            if exc.code == 429:
                try:
                    wait = float(exc.headers.get('Retry-After'))
                except (TypeError, ValueError):
                    wait = None
                if attempt < retries and wait is not None and wait <= max_rate_wait:
                    time.sleep(wait)
                    continue
                raise RateLimited(wait=wait, service=urllib.parse.urlsplit(url).netloc)
            last_error = exc
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            last_error = exc
        if attempt < retries:
            time.sleep(0.5 * (attempt + 1))
    raise last_error


# ---------------------------------------------------------------------------
#  Кэш результатов (чтобы не дёргать API повторно и не упираться в лимиты)
# ---------------------------------------------------------------------------

_CACHE = None
_CACHE_LOCK = threading.Lock()
_CACHE_MAX = 800
CACHE_FORMAT = 2   # увеличивайте при изменениях разбора: старый кэш будет сброшен


def _cache_fits(entry):
    """Запись кэша сделана текущей версией разбора."""
    return isinstance(entry, dict) and entry.get('t') and ('v' in entry)


def _cache_path():
    return os.path.join(app_dir(), CACHE_FILE)


def _load_cache():
    global _CACHE
    with _CACHE_LOCK:
        if _CACHE is None:
            data = {}
            try:
                with open(_cache_path(), 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if not isinstance(data, dict) or data.get('__format__') != CACHE_FORMAT:
                    data = {}   # кэш сделан другой версией разбора — обнуляем
            except (OSError, ValueError):
                data = {}
            data['__format__'] = CACHE_FORMAT
            _CACHE = data
        return _CACHE


def cache_get(key):
    """(найдено, значение); значение может быть None — «страницы нет»."""
    entry = _load_cache().get(key)
    if not entry or not _cache_fits(entry):
        return False, None
    ttl = 30 * 86400 if entry.get('v') is not None else 86400
    if time.time() - entry.get('t', 0) > ttl:
        return False, None
    return True, entry.get('v')


def cache_put(key, value):
    cache = _load_cache()
    with _CACHE_LOCK:
        cache[key] = {'t': time.time(), 'v': value}
        cache['__format__'] = CACHE_FORMAT
        real_keys = [k for k in cache if k != '__format__']
        if len(real_keys) > _CACHE_MAX:   # выбрасываем самые старые записи
            oldest = sorted(real_keys, key=lambda k: cache[k].get('t', 0))
            for k in oldest[:len(real_keys) - _CACHE_MAX]:
                cache.pop(k, None)
        try:
            tmp = _cache_path() + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(cache, f, ensure_ascii=False)
            os.replace(tmp, _cache_path())
        except OSError:
            pass


def cached(key, fn, *args, **kwargs):
    hit, value = cache_get(key)
    if hit:
        return value
    value = fn(*args, **kwargs)
    cache_put(key, value)
    return value


Attempt = namedtuple('Attempt', 'res kind wait')


def _attempt(fn, *args, **kwargs):
    """Вызов источника: kind = 'found' | 'missing' | 'error' | 'limited'."""
    try:
        res = fn(*args, **kwargs)
    except RateLimited as exc:
        return Attempt(None, 'limited', exc.wait)
    except NET_EXCEPTIONS:
        return Attempt(None, 'error', None)
    return Attempt(res, 'found', None) if res else Attempt(None, 'missing', None)


# ---------------------------------------------------------------------------
#  Очистка вики-разметки
# ---------------------------------------------------------------------------

# Шаблоны, которые полностью выбрасываются (примеры, этимология, транскрипция...)
DROP_TEMPLATES = {
    'пример', 'пример-пропущен', 'прим', 'примечание', 'семантика', 'этимология',
    'морфо', 'морфо-ru', 'морфемы', 'транскрипция', 'транскрипция-ru', 'произношение',
    'аудио', 'audio', 'по-слогам', 'по слогам', 'слоги', 'лексика', 'источник', 'sfn',
    'нп', 'категория', 'category', 'нее', 'фото', 'изображение', 'иллюстрация',
    'картинка', 'slim', 'clear', 'оформление', 'якорь', 'anchor', 'з', 'навигация',
    'лексема в викиданных', 'wikipedia', 'wikipediabox', 'википедия', 'викисловарь',
    'перенесено из википедии', 'к', 'ъ', 'ё', 'омонимы', 'improve', 'длина слова',
    'слобр', 'offensive', 'transcriptions-ru', 't-ru', 'wikidata', 'л', 'refless',
}

DROP_PREFIXES = ('этимолог', 'иллюстра', 'фото', 'картин', 'trans', 'itt', 'usex', 'uxi',
                 'якор', 'цитата', 'викисклад', 'commons', 'внешние ссылки', 'iw')

# Контекстные пометы: {{зоол.|ru}} -> (зоол.)
CONTEXT_MARKS = {
    'зоол.': 'зоол.', 'бот.': 'бот.', 'анат.': 'анат.', 'физиол.': 'физиол.',
    'мед.': 'мед.', 'психол.': 'психол.', 'псих.': 'психол.', 'хим.': 'хим.',
    'физ.': 'физ.', 'мат.': 'мат.', 'матем.': 'матем.', 'лингв.': 'лингв.',
    'грам.': 'грам.', 'муз.': 'муз.', 'спорт.': 'спорт.', 'техн.': 'техн.',
    'тех.': 'тех.', 'морск.': 'морск.', 'воен.': 'воен.', 'авиац.': 'авиац.',
    'астрон.': 'астрон.', 'геогр.': 'геогр.', 'геол.': 'геол.', 'экон.': 'экон.',
    'филос.': 'филос.', 'филол.': 'филол.', 'ист.': 'ист.', 'истор.': 'истор.',
    'религ.': 'религ.', 'церк.': 'церк.', 'кулин.': 'кулин.', 'прост.': 'прост.',
    'разг.': 'разг.', 'устар.': 'устар.', 'книжн.': 'книжн.', 'поэт.': 'поэт.',
    'публиц.': 'публиц.', 'научн.': 'научн.', 'ирон.': 'ирон.', 'шутл.': 'шутл.',
    'ласк.': 'ласк.', 'уменьш.': 'уменьш.', 'увелич.': 'увелич.', 'перен.': 'перен.',
    'спец.': 'спец.', 'обл.': 'обл.', 'диал.': 'диал.', 'сленг': 'сленг',
    'жарг.': 'жарг.', 'детск.': 'детск.', 'неодобр.': 'неодобр.', 'одобр.': 'одобр.',
    'презр.': 'презр.', 'пренебр.': 'пренебр.', 'груб.': 'груб.', 'эвф.': 'эвф.',
    'собир.': 'собир.', 'п.': 'перен.', 'биол.': 'биол.', 'геом.': 'геом.',
    'информ.': 'информ.', 'полит.': 'полит.', 'юр.': 'юр.', 'бухг.': 'бухг.',
    'кулинар.': 'кулин.', 'нар.-поэт.': 'нар.-поэт.', 'мол.': 'мол.',
    'сниж.': 'сниж.', 'уничиж.': 'уничиж.', 'обсц.': 'обсц.', 'бран.': 'бран.',
    'бранн.': 'бран.', 'ритор.': 'ритор.', 'неодуш.': 'неодуш.', 'одуш.': 'одуш.',
    'несов.': 'несов.', 'сов.': 'сов.', 'вульг.': 'вульг.', 'культурол.': 'культурол.',
    'социол.': 'социол.', 'игр.': 'игр.', 'комп.': 'комп.', 'прогр.': 'прогр.',
    'сет.': 'сет.', 'школьн.': 'школьн.', 'студ.': 'студ.', 'мифол.': 'мифол.',
    'библ.': 'библ.', 'охотн.': 'охотн.', 'рыб.': 'рыб.', 'пчел.': 'пчел.',
    'сад.': 'сад.', 'строит.': 'строит.', 'ж.-д.': 'ж.-д.', 'автомоб.': 'автомоб.',
    'косм.': 'косм.', 'генет.': 'генет.', 'биохим.': 'биохим.', 'фарм.': 'фарм.',
    'психол.': 'психол.', 'этногр.': 'этногр.', 'фольк.': 'фольк.', 'неол.': 'неол.',
}

# Служебные символы: {{-}} -> «-», {{--}} -> «—», {{!}} -> «|» и т. п.
DASH_LITERALS = {'-': '-', '--': '—', '---': '—', '—': '—'}
SPECIAL_TEXT = {'итп': ' и т. п.', 'итд': ' и т. д.', 'итпр': ' и т. п.',
                'nbsp': ' ', '~': ' ', '·': ' ', '!': '|', '!-': '|-'}

# Шаблоны, у которых оставляем последний аргумент как текст
KEEP_LAST_ARG = {
    'выдел', 'выделение', 'курсив', 'полужирный', 'em', 'i', 'b', 'strong', 'u', 's',
    'lang', 'язык', 'm', 'l', 'link', 'term', 'mention', 'abbr', 'tooltip', 'nobr',
    'small', 'span', 'font', 'big', 'sub', 'sup', 't', 'знач', 'значение',
    'знач.', 'tooltip-lang', 'шрифт', 'цвет', 'флэш', '==', '===', '====', 'wy',
}

# Шаблоны-термины: «t:=|слово» или «t:=|слово|пояснение»
DASH_JOIN_TEMPLATES = {'t:=', 't=', 'термин', 'term:'}

# Шаблоны с подписью-пометой: {{gloss|...}}, {{lb|en|...}}
LABEL_TEMPLATES = {'qualifier', 'lb', 'lbl', 'label', 'gloss', 'q', 'context',
                   'контекст', 'помета', 'пометка', 'mark'}


def split_template_args(inner):
    """Разбить содержимое шаблона по «|» верхнего уровня (учитывая {{...}} и [[...]])."""
    parts = []
    current = []
    braces = 0
    links = 0
    i = 0
    while i < len(inner):
        if inner.startswith('{{', i):
            braces += 1
            current.append('{{')
            i += 2
            continue
        if inner.startswith('}}', i):
            braces -= 1
            current.append('}}')
            i += 2
            continue
        if inner.startswith('[[', i):
            links += 1
            current.append('[[')
            i += 2
            continue
        if inner.startswith(']]', i):
            links -= 1
            current.append(']]')
            i += 2
            continue
        ch = inner[i]
        if ch == '|' and braces <= 0 and links <= 0:
            parts.append(''.join(current))
            current = []
        else:
            current.append(ch)
        i += 1
    parts.append(''.join(current))
    return parts


def handle_template(inner):
    """Преобразовать содержимое одного шаблона {{...}} в текст (или пустоту)."""
    parts = split_template_args(inner)
    name = re.sub(r'\s+', ' ', parts[0]).strip()
    low = name.lower()
    args = [p.strip() for p in parts[1:]]
    pos_args = [p for p in args if '=' not in p and p != '']
    last = pos_args[-1] if pos_args else ''

    if not low or low in DROP_TEMPLATES:
        return ''
    if any(low.startswith(p) for p in DROP_PREFIXES):
        return ''

    if low in DASH_LITERALS:
        return DASH_LITERALS[low]
    if low in SPECIAL_TEXT:
        return SPECIAL_TEXT[low]
    if low in ('"', '«', 'кавычки'):
        return '«%s»' % last if last else ''
    if low and set(low) <= {'='}:           # {{===|онанист}} — «онанист»
        return last
    if low in DASH_JOIN_TEMPLATES:          # {{t:=|термин|пояснение}}
        return ' — '.join(pos_args)
    if low == 'свойство':
        for a in reversed(pos_args):
            if re.fullmatch(r'[А-Яа-яЁёA-Za-z][А-Яа-яЁёA-Za-z\-]*', a):
                return 'свойство по значению прилагательного %s' % a
        return ''

    key = low if low.endswith('.') else low + '.'
    if key in CONTEXT_MARKS:
        return '(%s)' % CONTEXT_MARKS[key]

    base = low.rstrip('.')
    if base in LABEL_TEMPLATES:
        vals = [a for a in pos_args if not re.fullmatch(r'[a-z]{2,3}(-[a-z]+)?', a)]
        return '(%s)' % ', '.join(vals) if vals else ''

    if low == 'verb-dir':
        return ('→ ' + last) if last else ''
    if low in KEEP_LAST_ARG:
        return last

    # Обобщённая помета вида «мол.», «несов.», «техн.жарг.», «др.-рус.»
    if re.fullmatch(r'[А-Яа-яЁё][А-Яа-яЁё\-]{1,21}(?:\.[А-Яа-яЁё\-]{1,21})*\.', low):
        parts2 = low[:-1].split('.')          # убираем завершающую точку
        label = parts2[0]
        for p in parts2[1:]:
            label += ('.' if p.startswith('-') else '. ') + p
        return '(%s.)' % label

    return ''  # остальные неизвестные шаблоны выбрасываем


def render_templates(text, guard=400):
    """Раскрыть/убрать шаблоны, начиная с самых вложенных."""
    n = 0
    while '{{' in text and n < guard:
        n += 1
        m = re.search(r'\{\{([^{}]*)\}\}', text)
        if not m:
            break
        text = text[:m.start()] + handle_template(m.group(1)) + text[m.end():]
    return text


def clean_wiki(text):
    """Очистить фрагмент вики-разметки до читаемого текста."""
    text = re.sub(r'<ref[^>]*?/>', '', text)
    text = re.sub(r'<ref.*?</ref>', '', text, flags=re.S)
    text = re.sub(r'<[^>]+>', '', text)
    text = render_templates(text)
    text = re.sub(r'\[\[[^\[\]]*?\|([^\[\]]*?)\]\]', r'\1', text)
    text = re.sub(r'\[\[([^\[\]]*?)\]\]', r'\1', text)
    text = re.sub(r'\[(?:https?|ftp|s)://\S+\s+([^\]]+)\]', r'\1', text)
    text = re.sub(r'\[(?:https?|ftp)://\S+\]', '', text)
    text = text.replace("'''", '').replace("''", '')
    text = html.unescape(text)
    text = text.replace('\u00a0', ' ').replace('\u2009', ' ')
    text = re.sub(r'[{}]', '', text)
    text = re.sub(r'\(\s*\)', '', text)
    text = re.sub(r'\s+', ' ', text)
    text = re.sub(r'\s+([,;:.!?…])', r'\1', text)
    text = text.strip(' ;:—–-')
    if text and (text[-1].isalnum() or text[-1] in '»"\')'):
        text += '.'
    return text


def cut(text, limit):
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(' ', 1)[0].rstrip(' ,;:—–-') + '…'


# ---------------------------------------------------------------------------
#  Разбор статьи Викисловаря
# ---------------------------------------------------------------------------

HEAD_RE = re.compile(r'(?m)^(={1,6})[ \t]*(.+?)[ \t]*\1[ \t]*$')

POS_MAP = {
    'сущ': 'существительное', 'гл': 'глагол', 'прил': 'прилагательное',
    'нареч': 'наречие', 'мест': 'местоимение', 'числ': 'числительное',
    'предл': 'предлог', 'союз': 'союз', 'част': 'частица', 'межд': 'междометие',
    'предик': 'предикатив', 'предикатив': 'предикатив', 'вводн': 'вводное слово',
    'поговорк': 'поговорка', 'послов': 'пословица', 'фраз': 'фразеологизм',
    'аббр': 'аббревиатура', 'звукоподр': 'звукоподражание',
    'прич': 'причастие', 'деепр': 'деепричастие', 'мест-предик': 'местоимение-предикатив',
    # англоязычные разделы
    'noun': 'сущ.', 'verb': 'гл.', 'adjective': 'прил.', 'adverb': 'нареч.',
    'pronoun': 'мест.', 'preposition': 'предл.', 'conjunction': 'союз',
    'interjection': 'межд.', 'numeral': 'числ.', 'particle': 'частица',
    # короткие имена шаблонов в разделе морфологии ({{adv ru сравн}}, {{predic}}...)
    'adv': 'наречие', 'adj': 'прилагательное', 'pron': 'местоимение',
    'num': 'числительное', 'prep': 'предлог', 'conj': 'союз', 'intj': 'междометие',
    'predic': 'предикатив', 'part': 'частица', 'intro': 'вводное слово',
    'partic': 'причастие', 'ger': 'деепричастие', 'name': 'имя собственное',
    'comp': 'сравнительная степень',
}

EN_POS = {
    'noun': 'сущ.', 'verb': 'гл.', 'adjective': 'прил.', 'adverb': 'нареч.',
    'pronoun': 'мест.', 'preposition': 'предл.', 'conjunction': 'союз',
    'interjection': 'межд.', 'determiner': 'определитель', 'numeral': 'числ.',
    'article': 'артикль', 'particle': 'частица', 'exclamation': 'воскл.',
    'contraction': 'сокращение', 'abbreviation': 'аббревиатура', 'prefix': 'приставка',
    'suffix': 'суффикс', 'phrase': 'фраза', 'proper noun': 'имя собственное',
}

# Слова для описания грамматических форм ({{Форма-гл}}, {{Форма-сущ}} и т. п.)
FORM_WORDS = {
    'время': {'п': 'прошедшего времени', 'прош': 'прошедшего времени',
              'н': 'настоящего времени', 'наст': 'настоящего времени',
              'б': 'будущего времени', 'буд': 'будущего времени'},
    'род': {'м': 'мужского рода', 'ж': 'женского рода', 'с': 'среднего рода'},
    'число': {'ед': 'единственного числа', '1': 'единственного числа',
              's': 'единственного числа', 'мн': 'множественного числа',
              '2': 'множественного числа', 'p': 'множественного числа'},
    'лицо': {'1': '1-го лица', '2': '2-го лица', '3': '3-го лица'},
    'накл': {'и': 'изъявительного наклонения', 'п': 'повелительного наклонения',
             'с': 'сослагательного наклонения'},
    'залог': {'действ': 'действительного залога', 'страд': 'страдательного залога'},
    'падеж': {'и': 'именительного падежа', 'р': 'родительного падежа',
              'д': 'дательного падежа', 'в': 'винительного падежа',
              'т': 'творительного падежа', 'п': 'предложного падежа',
              'з': 'звательного падежа', 'м': 'местного падежа',
              'местн': 'местного падежа', 'парт': 'партитивного падежа'},
    'сравн': {'1': 'сравнительной степени', 'сравн': 'сравнительной степени',
              'да': 'сравнительной степени'},
}

FORM_KIND_LABEL = {
    'гл': 'форма глагола', 'сущ': 'форма существительного',
    'прил': 'форма прилагательного', 'мест': 'форма местоимения',
    'числ': 'форма числительного', 'нареч': 'форма наречия',
    'прич': 'форма причастия', 'деепр': 'форма деепричастия',
    'предик': 'форма предикатива', 'част': 'форма частицы',
}

FORM_KIND_WORD = {
    'гл': 'глагола', 'сущ': 'существительного', 'прил': 'прилагательного',
    'мест': 'местоимения', 'числ': 'числительного', 'нареч': 'наречия',
    'предик': 'предикатива', 'част': 'частицы', 'межд': 'междометия',
}


def iter_sections(text, levels):
    """Заголовки вики-текста заданных уровней и их тела."""
    marks = [(m.start(), m.end(), len(m.group(1)), m.group(2).strip())
             for m in HEAD_RE.finditer(text)]
    for i, (_start, end, lvl, title) in enumerate(marks):
        if lvl not in levels:
            continue
        stop = len(text)
        for s2, _e2, lvl2, _t2 in marks[i + 1:]:
            if lvl2 <= lvl:
                stop = s2
                break
        yield lvl, title, text[end:stop]


def iter_sections_pos(text, levels):
    """То же, но с позицией заголовка (для сохранения порядка блоков)."""
    marks = [(m.start(), m.end(), len(m.group(1)), m.group(2).strip())
             for m in HEAD_RE.finditer(text)]
    for i, (start, end, lvl, title) in enumerate(marks):
        if lvl not in levels:
            continue
        stop = len(text)
        for s2, _e2, lvl2, _t2 in marks[i + 1:]:
            if lvl2 <= lvl:
                stop = s2
                break
        yield start, lvl, title, text[end:stop]


def section_lang(title):
    """Название языкового раздела: «{{-ru-|nocat}}» -> «-ru-», «Русский» -> «русский»."""
    t = title.strip()
    m = re.match(r'\{\{\s*([^|}]+)', t)
    if m:
        t = m.group(1)
    return t.strip('{} ').strip().lower()


def find_ru_section(wikitext):
    """Тело русского раздела: `= {{-ru-}} =`, `= {{-ru-|nocat}} =` или `== Русский ==`."""
    for _lvl, title, body in iter_sections(wikitext, (1, 2)):
        if section_lang(title) in ('-ru-', 'русский', 'ru'):
            return body
    return None


def pos_from_homonym(title):
    """Часть речи из заголовка омонима: «{{з|(наречие I)}}» -> «наречие»."""
    m = re.search(r'\(([^()]+)\)', title)
    text = m.group(1) if m else title
    text = re.sub(r'\s+[IVXLC]+$', '', text).strip(' .').lower()
    return text or None


def detect_pos(segment):
    """Часть речи по имени шаблона в разделе морфологии."""
    for m in re.finditer(r'\{\{\s*([^|{}\s]+)', segment):
        stem = re.split(r'[-\s]', m.group(1).lower())[0]
        if stem in POS_MAP:
            return POS_MAP[stem]
    return None


def extract_defs(segment, limit=420):
    """Строки-определения (# ...) из раздела «Значение»: [{'text': ..., 'example': None}]."""
    out = []
    for line in segment.split('\n'):
        s = line.strip()
        if not s.startswith('# '):
            continue
        t = clean_wiki(s[2:].strip())
        # Если после удаления помет «(разг.)», «(груб.)» и знаков препинания
        # не осталось ни букв, ни цифр — это не толкование, а одни пометы.
        core = re.sub(r'\([^()]*\)', '', t)
        core = re.sub(r'[^А-Яа-яЁёA-Za-z0-9]', '', core)
        if core:
            out.append({'text': cut(t, limit), 'example': None})
    return out


# ------------------------- разбор грамматических форм -------------------------

def find_named_templates(text, prefixes):
    """Найти шаблоны верхнего уровня, имя которых начинается с одного из префиксов."""
    results = []
    i = 0
    n = len(text)
    while i < n - 1:
        start = text.find('{{', i)
        if start < 0:
            break
        depth = 0
        j = start
        closed = False
        while j < n - 1:
            if text.startswith('{{', j):
                depth += 1
                j += 2
                continue
            if text.startswith('}}', j):
                depth -= 1
                j += 2
                if depth == 0:
                    closed = True
                    break
                continue
            j += 1
        if not closed:
            break
        inner = text[start + 2:j - 2]
        name = split_template_args(inner)[0].strip().lower()
        if any(name.startswith(p) for p in prefixes):
            results.append((start, inner))
            i = j
        else:
            i = start + 2
    return results


def parse_form_template(inner):
    """Разобрать {{Форма-гл|...}} / {{Форма-сущ|...}} в подпись и описание.

    Возвращает (заголовок блока, текст) или None.
    """
    parts = split_template_args(inner)
    name = re.sub(r'\s+', ' ', parts[0]).strip().lower()
    named = {}
    positional = []
    for arg in parts[1:]:
        arg = arg.strip()
        m = re.match(r'^([^\s=|]+)\s*=\s*(.*)$', arg, re.S)
        if m:
            named[m.group(1).lower()] = m.group(2).strip()
        else:
            positional.append(arg)

    def val(*keys):
        for k in keys:
            if named.get(k):
                return named[k]
        return ''

    def pos_arg(idx):
        return positional[idx] if len(positional) > idx else ''

    kind = name.split('-', 1)[1].strip() if '-' in name else ''
    base = (val('база', 'слово', 'лексема') or pos_arg(0)).split('#')[0].strip()
    base = clean_wiki(base).rstrip('.') if base else ''

    def clean_no_dot(text):
        """Очистить вики-текст и убрать точку в конце (она тут лишняя)."""
        cleaned = clean_wiki(text)
        return cleaned[:-1] if cleaned.endswith('.') else cleaned

    mark = clean_no_dot(val('помета')) if val('помета') else ''
    znach = clean_no_dot(val('знач', 'значение')) if val('знач', 'значение') else ''
    komment = clean_no_dot(val('коммент', 'комментарий')) if val('коммент', 'комментарий') else ''

    descriptors = []

    def add(group, value):
        v = (value or '').strip().lower()
        if not v:
            return
        word = FORM_WORDS.get(group, {}).get(v)
        if word:
            descriptors.append(word)

    if kind == 'гл':
        add('время', val('время') or pos_arg(1))
        add('род', val('род') or pos_arg(3))
        add('число', val('число') or pos_arg(2))
        add('лицо', val('лицо'))
        add('накл', val('накл'))
        add('залог', val('залог'))
        for key, word in (('прич', 'причастие'), ('деепр', 'деепричастие'),
                          ('кр', 'краткой формы')):
            if val(key):
                descriptors.append(word)
    else:
        add('падеж', val('падеж') or pos_arg(1))
        add('род', val('род') or pos_arg(3))
        add('число', val('число') or pos_arg(2))
        add('сравн', val('сравн'))
        if val('кр'):
            descriptors.append('краткой формы')

    kind_word = FORM_KIND_WORD.get(kind, '')
    if descriptors:
        text = 'форма %s %s' % (', '.join(descriptors), kind_word or 'слова')
    elif kind_word:
        text = 'форма %s' % kind_word
    else:
        text = 'форма слова'
    if base:
        text += ' «%s»' % base
    if mark:
        text = '%s %s' % (mark, text)
    if komment:
        text = '%s (%s)' % (text, komment)
    if znach:
        text = '%s (значение: %s)' % (text, znach)
    if not base and not descriptors and not kind_word:
        return None
    return FORM_KIND_LABEL.get(kind, 'форма слова'), text


def extract_form_blocks(body):
    """Блоки «форма слова» в порядке появления в статье -> [(позиция, блок)]."""
    out = []
    for position, inner in find_named_templates(body, ('форма', 'форм:')):
        parsed = parse_form_template(inner)
        if not parsed:
            continue
        label, text = parsed
        out.append((position, {'pos': label,
                               'defs': [{'text': cut(text, 380), 'example': None}]}))
    return out


def parse_ru_section(wikitext, max_defs=5):
    """Разобрать русскую статью в блоки [{часть речи, значения}] по порядку статьи."""
    body = find_ru_section(wikitext)
    if body is None:
        return []
    items = []
    cur_pos = None
    homonym_pos = None
    for position, lvl, title, segment in iter_sections_pos(body, (2, 3, 4)):
        t = title.strip()
        if lvl == 2:
            homonym_pos = pos_from_homonym(t)   # «(наречие I)» -> «наречие»
            cur_pos = None
        elif 'Морфологическ' in t:
            pos = detect_pos(segment)
            if pos:
                cur_pos = pos
        elif t.startswith('Значение'):
            defs = extract_defs(segment)
            if defs:
                items.append((position, {'pos': cur_pos or homonym_pos,
                                         'defs': defs[:max_defs]}))
    items.extend(extract_form_blocks(body))
    items.sort(key=lambda pair: pair[0])

    blocks = []
    for _position, block in items:
        pos = block.get('pos') or ''
        if blocks and blocks[-1].get('pos') == block.get('pos') and pos.startswith('форма'):
            blocks[-1]['defs'].extend(block['defs'])   # объединяем формы одного вида
        else:
            blocks.append(block)
    return blocks


def parse_enwiktionary(wikitext, lang_title):
    """Раздел нужного языка в англ. Викисловаре (запасной источник)."""
    body = None
    for _lvl, title, sec in iter_sections(wikitext, (2,)):
        if title.strip().lower() == lang_title.lower():
            body = sec
            break
    if body is None:
        return []
    blocks = []
    for _lvl, title, segment in iter_sections(body, (3,)):
        t = title.strip()
        pos = POS_MAP.get(t.lower())
        if pos is None:
            continue
        defs = extract_defs(segment, limit=380)
        if defs:
            blocks.append({'pos': pos, 'defs': defs[:4]})
    return blocks

# ---------------------------------------------------------------------------
#  Источники данных (с кэшем на диске)
# ---------------------------------------------------------------------------

def fetch_ru_word(word, max_defs=5, timeout=10):
    """Статья из русского Викисловаря -> результат или None."""
    return cached('ru:%s' % word.lower(), _fetch_ru_word_net, word, 10, timeout)


def fetch_enwiki_word(word, lang_title, max_defs=4, timeout=10):
    """Раздел нужного языка в англ. Викисловаре -> результат или None."""
    return cached('enw:%s:%s' % (lang_title.lower(), word.lower()),
                  _fetch_enwiki_word_net, word, lang_title, 8, timeout)


def fetch_en_dictapi(word, timeout=8):
    """Английское слово через dictionaryapi.dev -> результат или None."""
    return cached('dapi:%s' % word.lower(), _fetch_en_dictapi_net, word, timeout)


# ---------------------------------------------------------------------------
#  Источники данных
# ---------------------------------------------------------------------------

def wiki_page(site, word, timeout=10):
    """Вики-текст статьи с запасной попыткой в нижнем регистре первой буквы."""
    variants = [word]
    if word[:1].isupper():
        variants.append(word[0].lower() + word[1:])
    for variant in variants:
        try:
            data = http_get_json('https://%s/w/api.php' % site,
                                 {'action': 'parse', 'page': variant, 'prop': 'wikitext',
                                  'format': 'json', 'formatversion': '2', 'redirects': '1'},
                                 timeout=timeout)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:  # страницы нет — это не сетевая ошибка
                continue
            raise
        if 'error' in data:
            continue
        parse = data.get('parse') or {}
        if parse.get('wikitext'):
            return parse['wikitext'], parse.get('title', variant)
    return None, None


def _fetch_ru_word_net(word, max_defs=5, timeout=10):
    """Статья из русского Викисловаря (сетевой запрос)."""
    wikitext, title = wiki_page('ru.wiktionary.org', word, timeout=timeout)
    if wikitext is None:
        return None
    blocks = parse_ru_section(wikitext, max_defs=max_defs)
    if not blocks:
        return None
    return {'word': title, 'lang': 'ru', 'source': 'ru.wiktionary.org', 'blocks': blocks}


def _fetch_enwiki_word_net(word, lang_title, max_defs=4, timeout=10):
    """Раздел нужного языка в англ. Викисловаре (сетевой запрос)."""
    wikitext, title = wiki_page('en.wiktionary.org', word, timeout=timeout)
    if wikitext is None:
        return None
    blocks = parse_enwiktionary(wikitext, lang_title)
    if not blocks:
        return None
    res = {'word': title, 'lang': 'ru' if lang_title == 'Russian' else 'en',
           'source': 'en.wiktionary.org', 'blocks': blocks}
    if lang_title == 'Russian':
        res['note'] = ('В русском Викисловаре статьи не нашлось; '
                       'ниже — толкование из английского Wiktionary (на английском).')
    return res


def _fetch_en_dictapi_net(word, timeout=8):
    """Английское слово через dictionaryapi.dev (сетевой запрос)."""
    url = 'https://api.dictionaryapi.dev/api/v2/entries/en/' + urllib.parse.quote(word)
    try:
        data = http_get_json(url, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    if not isinstance(data, list) or not data:
        return None
    phonetic = None
    blocks = []
    for entry in data:
        if not phonetic:
            phonetic = entry.get('phonetic')
            if not phonetic:
                for ph in entry.get('phonetics') or []:
                    if ph.get('text'):
                        phonetic = ph['text']
                        break
        for meaning in entry.get('meanings') or []:
            raw_pos = (meaning.get('partOfSpeech') or '').strip()
            pos = EN_POS.get(raw_pos.lower(), raw_pos.capitalize() if raw_pos else 'значение')
            defs = []
            for d in meaning.get('definitions') or []:
                text = (d.get('definition') or '').strip()
                if not text:
                    continue
                example = (d.get('example') or '').strip() or None
                defs.append({'text': cut(text, 380),
                             'example': cut(example, 300) if example else None})
            if defs:
                blocks.append({'pos': pos, 'defs': defs[:4]})
    if not blocks:
        return None
    return {'word': word, 'lang': 'en', 'source': 'dictionaryapi.dev',
            'phonetic': phonetic, 'blocks': blocks[:4]}


def spell_suggest(word, timeout=8):
    """Варианты исправления опечатки (Яндекс.Спеллер). Сортировка — по алфавиту."""
    return cached('spell:%s' % word.lower(), _spell_suggest_net, word, timeout)


def _spell_suggest_net(word, timeout=8):
    data = http_get_json(YA_SPELLER, {'text': word, 'lang': 'ru,en'}, timeout=timeout)
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        for s in sorted(item.get('s') or []):
            if s and s not in out:
                out.append(s)
    return out


def opensearch(word, lang, timeout=8):
    """Подсказки Викисловаря («возможно, вы имели в виду...»)."""
    return cached('os:%s:%s' % (lang, word.lower()), _opensearch_net, word, lang, timeout)


def _opensearch_net(word, lang, timeout=8):
    site = 'ru.wiktionary.org' if lang == 'ru' else 'en.wiktionary.org'
    data = http_get_json('https://%s/w/api.php' % site,
                         {'action': 'opensearch', 'search': word, 'limit': '6',
                          'namespace': '0', 'format': 'json'}, timeout=timeout)
    if not isinstance(data, list) or len(data) < 2:
        return []
    return [s for s in data[1] if s.lower() != word.lower()]


# ---------------------------------------------------------------------------
#  Главная логика поиска
# ---------------------------------------------------------------------------

def has_cyrillic(text):
    return bool(re.search(r'[А-Яа-яЁё]', text))


def detect_lang(word):
    return 'ru' if has_cyrillic(word) else 'en'


def normalize_query(word):
    word = (word or '').strip()
    word = re.sub(r'\s+', ' ', word)
    word = word.strip(' \t.,;:!?()[]{}«»"\'`*\u2014\u2013-')
    return word


def filter_candidates(cands, query):
    """Убрать пустые/совпадающие/чужеродные варианты (напр. украинские для русского)."""
    if has_cyrillic(query) and not any(ch in query.lower() for ch in 'іїєґў'):
        foreign = 'іїєґў'
    else:
        foreign = ''
    out = []
    for s in cands or []:
        s = (s or '').strip()
        if not s or s.lower() == query.lower():
            continue
        if has_cyrillic(s) != has_cyrillic(query):
            continue
        if foreign and any(ch in s.lower() for ch in foreign):
            continue
        if s not in out:
            out.append(s)
    return out


def trim_blocks(res, max_defs):
    """Оставить не больше max_defs значений в каждом блоке (кэш хранит больше)."""
    for block in res.get('blocks', []):
        block['defs'] = block.get('defs', [])[:max_defs]
    return res


def lookup_exact(word, lang, cfg):
    """Поиск без исправлений.

    Возвращает (результат|None, доступна ли сеть, сколько ждать при лимите 429).
    """
    max_defs = int(cfg.get('max_defs', 5))
    timeout = int(cfg.get('timeout', 10))
    if lang == 'ru':
        sources = [(fetch_ru_word, (word, max_defs, timeout)),
                   (fetch_enwiki_word, (word, 'Russian', max_defs, timeout))]
    else:
        sources = [(fetch_en_dictapi, (word, timeout)),
                   (fetch_enwiki_word, (word, 'English', max_defs, timeout))]
    reachable = False
    limit_wait = None
    for fn, args in sources:
        att = _attempt(fn, *args)
        if att.kind == 'found':
            return trim_blocks(att.res, max_defs), True, None
        if att.kind == 'limited':
            reachable = True
            if limit_wait is None or (att.wait or 0) > limit_wait:
                limit_wait = att.wait or 15
        elif att.kind == 'missing':
            reachable = True
    return None, reachable, limit_wait


def lookup(word, cfg=None, correct=True):
    """Полный цикл: язык -> поиск (и проверка опечаток параллельно) -> результат."""
    cfg = cfg or DEFAULT_CONFIG
    query = normalize_query(word)
    if not query:
        return {'query': word, 'error': 'Пустой запрос.'}

    lang = detect_lang(query)
    timeout = int(cfg.get('timeout', 10))
    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_exact = pool.submit(lookup_exact, query, lang, cfg)
        fut_spell = pool.submit(_attempt, spell_suggest, query, timeout) if correct else None
        target_res, net_ok, limit_wait = fut_exact.result()
        if fut_spell is not None:
            spell_res = fut_spell.result().res
        else:
            spell_res = []

    candidates = filter_candidates(spell_res, query)

    # Слово найдено с первой попытки
    if target_res:
        if candidates:
            target_res['suggestions'] = candidates[:4]
        target_res['query'] = query
        return target_res

    # Похоже на опечатку — пробуем первый вариант исправления
    if candidates:
        fixed, fixed_ok, fixed_limit = lookup_exact(candidates[0], detect_lang(candidates[0]), cfg)
        net_ok = net_ok or fixed_ok
        if fixed_limit is not None and limit_wait is None:
            limit_wait = fixed_limit
        if fixed:
            fixed['fixed'] = {'from': query, 'to': fixed.get('word', candidates[0])}
            fixed['query'] = query
            return fixed

    if not net_ok:
        return {'query': query,
                'error': 'Нет соединения с интернетом — проверьте сеть и попробуйте снова.'}

    if limit_wait is not None:
        return {'query': query,
                'error': ('Онлайн-словарь ограничил частоту запросов. '
                          'Повторите через ~%d сек.' % int(limit_wait + 1))}

    suggestions = list(candidates)
    extra = _attempt(opensearch, query, lang, timeout)
    for s in extra.res or []:
        if s not in suggestions:
            suggestions.append(s)
    suggestions = filter_candidates(suggestions, query)[:6]

    return {'query': query, 'word': query, 'lang': lang, 'notfound': True,
            'suggestions': suggestions,
            'source': 'ru.wiktionary.org' if lang == 'ru' else 'dictionaryapi.dev'}


# ---------------------------------------------------------------------------
#  Текстовый вывод (--cli и «Копировать результат»)
# ---------------------------------------------------------------------------

def format_text(res):
    lines = []
    q = res.get('query') or res.get('word') or ''
    if res.get('error'):
        return '⚠️  ' + res['error']
    if res.get('notfound'):
        lines.append('😕  Ничего не нашлось по запросу «%s»' % q)
        if res.get('suggestions'):
            lines.append('Возможно, вы имели в виду: ' + ', '.join(res['suggestions']))
        return '\n'.join(lines)
    if res.get('fixed'):
        lines.append('✏️  Исправлено: %s (вы ввели «%s»)' % (res['fixed']['to'], res['fixed']['from']))
    head = '📖  ' + str(res.get('word', q))
    if res.get('phonetic'):
        head += '  [%s]' % res['phonetic']
    lines.append(head)
    if res.get('suggestions') and not res.get('fixed'):
        lines.append('Возможно, вы имели в виду: ' + ', '.join(res['suggestions']))
    for block in res.get('blocks', []):
        lines.append('  %s:' % (block.get('pos') or 'значение'))
        for i, d in enumerate(block.get('defs', []), 1):
            lines.append('    %d. %s' % (i, d['text']))
            if d.get('example'):
                lines.append('       пример: %s' % d['example'])
    if res.get('note'):
        lines.append('  ' + res['note'])
    lines.append('  Источник: %s' % res.get('source', '—'))
    return '\n'.join(lines)


def run_cli(word, as_json=False, correct=True):
    res = lookup(word, DEFAULT_CONFIG, correct=correct)
    if as_json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(format_text(res))


# ---------------------------------------------------------------------------
#  Конфигурация и журнал
# ---------------------------------------------------------------------------

def app_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    path = os.path.join(app_dir(), CONFIG_FILE)
    try:
        with open(path, 'r', encoding='utf-8') as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        save_config(cfg)
    return cfg


def save_config(cfg):
    try:
        with open(os.path.join(app_dir(), CONFIG_FILE), 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def log_exception():
    import traceback
    try:
        with open(os.path.join(app_dir(), LOG_FILE), 'a', encoding='utf-8') as f:
            f.write(traceback.format_exc() + '\n')
    except OSError:
        pass


# ---------------------------------------------------------------------------
#  Окно приложения (tkinter)
# ---------------------------------------------------------------------------

FONT = 'Segoe UI'

COLORS = {
    'bg': '#17181d', 'input': '#22242c', 'border': '#333748',
    'fg': '#e8eaf0', 'muted': '#8b93a7', 'accent': '#7aa2ff', 'link': '#93b8ff',
    'green': '#63d29a', 'red': '#ff7b72', 'fix': '#f0b46b',
}


class SlovarikApp:
    def __init__(self, cfg):
        self.cfg = cfg
        self.scale = dpi_scale()
        self.jobs = queue.Queue()
        self.seq = 0
        self.visible = False
        self.link_targets = {}
        self.last_plain = ''
        self._drag = (0, 0)
        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.configure(bg=COLORS['border'])
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.attributes('-topmost', True)
        self._build()
        self._bind()
        self._place()

    # ------------------------------ интерфейс ------------------------------
    def px(self, value):
        return int(round(value * self.scale))

    def _build(self):
        c = COLORS
        body = tk.Frame(self.root, bg=c['bg'])
        body.pack(fill='both', expand=True, padx=1, pady=1)

        # Заголовок (за него можно тянуть окно)
        bar = tk.Frame(body, bg=c['bg'])
        bar.pack(fill='x', padx=14, pady=(10, 2))
        title = tk.Label(bar, text='📖  ' + APP_NAME, bg=c['bg'], fg=c['fg'],
                         font=(FONT, 11, 'bold'))
        title.pack(side='left')
        for widget in (bar, title):
            widget.bind('<Button-1>', self._drag_start)
            widget.bind('<B1-Motion>', self._drag_move)
        self.btn_close = tk.Label(bar, text='✕', bg=c['bg'], fg=c['muted'],
                                  font=(FONT, 11, 'bold'), cursor='hand2', padx=6)
        self.btn_close.pack(side='right')
        self.btn_close.bind('<Button-1>', lambda e: self.hide())
        self.btn_close.bind('<Enter>', lambda e: self.btn_close.config(fg=c['red']))
        self.btn_close.bind('<Leave>', lambda e: self.btn_close.config(fg=c['muted']))

        # Строка ввода
        row = tk.Frame(body, bg=c['bg'])
        row.pack(fill='x', padx=14, pady=(8, 0))
        self.entry = tk.Entry(row, font=(FONT, 13), bg=c['input'], fg=c['fg'],
                              insertbackground=c['accent'], relief='flat',
                              highlightthickness=1, highlightbackground=c['border'],
                              highlightcolor=c['accent'])
        self.entry.pack(side='left', fill='x', expand=True, ipady=self.px(5))
        find = tk.Button(row, text='Найти', command=self.start_search, relief='flat',
                         bg=c['input'], fg=c['accent'], activebackground=c['accent'],
                         activeforeground='#0e1015', font=(FONT, 10, 'bold'),
                         cursor='hand2', padx=12, bd=0, highlightthickness=0)
        find.pack(side='left', padx=(8, 0), ipady=self.px(4))

        # Подсказка
        hint = tk.Label(body, text='Enter — найти  ·  Esc — скрыть  ·  Ctrl+Space — показать/скрыть',
                        bg=c['bg'], fg=c['muted'], font=(FONT, 8), anchor='w')
        hint.pack(fill='x', padx=16, pady=(4, 6))

        # Статус
        self.status = tk.Label(body, text='Готово. Напишите слово и нажмите Enter.',
                               bg=c['bg'], fg=c['accent'], font=(FONT, 9), anchor='w')
        self.status.pack(fill='x', padx=16)

        tk.Frame(body, bg=c['border'], height=1).pack(fill='x', padx=14, pady=6)

        # Область результата
        box = tk.Frame(body, bg=c['bg'])
        box.pack(fill='both', expand=True, padx=(14, 8), pady=(0, 12))
        self.text = tk.Text(box, wrap='word', bd=0, relief='flat', bg=c['bg'],
                            fg=c['fg'], font=(FONT, 10), height=10, cursor='arrow',
                            padx=2, pady=0, spacing1=1, insertwidth=0, takefocus=0)
        scroll = tk.Scrollbar(box, command=self.text.yview, width=self.px(10))
        self.text.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.text.pack(side='left', fill='both', expand=True)

        self.text.tag_configure('title', font=(FONT, 12, 'bold'),
                                foreground=c['accent'], spacing3=4)
        self.text.tag_configure('pos', font=(FONT, 10, 'bold'),
                                foreground=c['green'], spacing1=8)
        self.text.tag_configure('def', font=(FONT, 10),
                                lmargin1=self.px(14), lmargin2=self.px(26))
        self.text.tag_configure('ex', font=(FONT, 9, 'italic'), foreground=c['muted'],
                                lmargin1=self.px(30), lmargin2=self.px(38))
        self.text.tag_configure('muted', font=(FONT, 8), foreground=c['muted'], spacing1=6)
        self.text.tag_configure('err', foreground=c['red'])
        self.text.tag_configure('fix', foreground=c['fix'], font=(FONT, 10, 'bold'))
        self.text.tag_configure('link', foreground=c['link'], underline=True, font=(FONT, 10))
        self.text.configure(state='disabled')

        # Контекстное меню
        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label='Копировать результат', command=self.copy_result)
        self.menu.add_command(label='Очистить', command=self.clear)
        self.menu.add_separator()
        self.menu.add_command(label='Скрыть\tEsc', command=self.hide)
        self.menu.add_command(label='Выход\tCtrl+Q', command=self.quit)

    def _bind(self):
        self.root.bind('<Escape>', lambda e: self.hide())
        self.root.bind('<Control-q>', lambda e: self.quit())
        self.root.bind('<Return>', lambda e: self.start_search())
        self.root.bind('<Button-3>', self._popup_menu)
        self.entry.bind('<FocusIn>',
                        lambda e: self.entry.configure(highlightbackground=COLORS['accent']))
        self.entry.bind('<FocusOut>',
                        lambda e: self.entry.configure(highlightbackground=COLORS['border']))
        self.entry.bind('<Control-a>', self._select_all)
        self.entry.bind('<Control-A>', self._select_all)
        self.entry.bind('<Control-l>', self._select_all)
        self.entry.bind('<Control-L>', self._select_all)
        self.text.tag_bind('link', '<Button-1>', self._on_link_click)
        self.text.tag_bind('link', '<Enter>', lambda e: self.text.configure(cursor='hand2'))
        self.text.tag_bind('link', '<Leave>', lambda e: self.text.configure(cursor='arrow'))

    def _place(self):
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        w = self.px(self.cfg.get('width', 480))
        h = self.px(self.cfg.get('height', 410))
        x = max(0, (sw - w) // 2)
        y = max(0, int(sh * float(self.cfg.get('top_ratio', 0.16))))
        self.root.geometry('%dx%d+%d+%d' % (w, h, x, y))

    # --------------------------- перетаскивание ----------------------------
    def _drag_start(self, event):
        self._drag = (event.x_root - self.root.winfo_x(),
                      event.y_root - self.root.winfo_y())

    def _drag_move(self, event):
        dx, dy = self._drag
        self.root.geometry('+%d+%d' % (event.x_root - dx, event.y_root - dy))

    def _popup_menu(self, event):
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    # ------------------------------ видимость ------------------------------
    def show(self):
        self._place()
        self.root.deiconify()
        self.root.attributes('-topmost', True)
        self.root.lift()
        try:
            self.root.focus_force()
        except Exception:
            pass
        self.entry.focus_force()
        self.entry.selection_range(0, 'end')
        self.visible = True
        self._fade_in(float(self.cfg.get('opacity', 0.98)))

    def _fade_in(self, target):
        try:
            self.root.attributes('-alpha', 0.15)
        except Exception:
            return

        def step(alpha):
            try:
                if not self.visible:
                    return
                self.root.attributes('-alpha', alpha)
                if alpha < target - 0.01:
                    self.root.after(16, lambda: step(min(target, alpha + target / 8.0)))
            except Exception:
                pass

        self.root.after(10, lambda: step(target / 6.0))

    def hide(self):
        if not self.visible:
            return
        self.visible = False
        try:
            self.root.attributes('-alpha', 0.0)
        except Exception:
            pass
        self.root.withdraw()

    def toggle(self):
        self.hide() if self.visible else self.show()

    def quit(self):
        try:
            import keyboard
            keyboard.unhook_all()
        except Exception:
            pass
        self.root.destroy()

    # ------------------------------- поиск ---------------------------------
    def clear(self):
        self.entry.delete(0, 'end')
        self._render([('Напишите слово и нажмите Enter.\n', 'muted')],
                     'Напишите слово и нажмите Enter.')
        self.status.configure(text='Готово.', foreground=COLORS['accent'])

    def start_search(self, correct=True):
        word = self.entry.get().strip()
        if not word:
            self.status.configure(text='Введите слово 🙂', foreground=COLORS['fix'])
            return
        self.seq += 1
        self.status.configure(text='Ищу «%s»…' % word, foreground=COLORS['accent'])
        self._render([('…\n', 'muted')], '')
        threading.Thread(target=self._worker, args=(self.seq, word, correct),
                         daemon=True).start()

    def _worker(self, seq, word, correct):
        try:
            res = lookup(word, self.cfg, correct=correct)
        except Exception as exc:  # фоновая нить не должна падать молча
            log_exception()
            res = {'query': word, 'error': 'Внутренняя ошибка: %s' % exc}
        self.jobs.put(('result', (seq, res)))

    def _poll(self):
        try:
            while True:
                kind, payload = self.jobs.get_nowait()
                if kind == 'toggle':
                    self.toggle()
                elif kind == 'result':
                    seq, res = payload
                    if seq == self.seq:
                        self.render_result(res)
        except queue.Empty:
            pass
        self.root.after(50, self._poll)

    # ------------------------------ отрисовка ------------------------------
    def _render(self, segments, plain):
        self.last_plain = plain
        self.text.configure(state='normal')
        self.text.delete('1.0', 'end')
        for seg_text, tag in segments:
            self.text.insert('end', seg_text, tag)
        self.text.configure(state='disabled')
        self.text.see('1.0')

    def _add_link(self, segments, word, label=None, raw=False):
        tag = 'link%d' % len(self.link_targets)
        self.link_targets[tag] = (word, raw)
        segments.append((label or word, ('link', tag)))

    def _select_all(self, _event=None):
        self.entry.selection_range(0, 'end')
        self.entry.icursor('end')
        return 'break'

    def _on_link_click(self, _event):
        index = self.text.index('@%d,%d' % (
            self.text.winfo_pointerx() - self.text.winfo_rootx(),
            self.text.winfo_pointery() - self.text.winfo_rooty()))
        for tag in self.text.tag_names(index):
            if tag in self.link_targets:
                word, raw = self.link_targets[tag]
                self.entry.delete(0, 'end')
                self.entry.insert(0, word)
                self.start_search(correct=not raw)
                return

    def render_result(self, res):
        c = COLORS
        segments = []
        self.link_targets = {}

        if res.get('error'):
            self.status.configure(text='Ошибка', foreground=c['red'])
            segments.append(('⚠️  %s\n' % res['error'], 'err'))
            self._render(segments, format_text(res))
            return

        if res.get('notfound'):
            q = res.get('query') or ''
            self.status.configure(text='Не найдено', foreground=c['fix'])
            segments.append(('😕  Ничего не нашлось по запросу «%s»\n' % q, 'title'))
            if res.get('suggestions'):
                segments.append(('Возможно, вы имели в виду: ', 'muted'))
                for i, s in enumerate(res['suggestions']):
                    if i:
                        segments.append((' · ', 'muted'))
                    self._add_link(segments, s)
            else:
                segments.append(('Проверьте раскладку клавиатуры или попробуйте другое слово.',
                                 'muted'))
            self._render(segments, format_text(res))
            return

        self.status.configure(text='Найдено', foreground=c['green'])

        fixed = res.get('fixed')
        if fixed:
            segments.append(('✏️  Исправлено: %s\n' % fixed['to'], 'fix'))
            segments.append(('вы ввели «%s» — ' % fixed['from'], 'muted'))
            self._add_link(segments, fixed['from'], 'искать как есть', raw=True)
            segments.append(('\n', 'muted'))

        head = '📖  %s' % res.get('word', '')
        if res.get('phonetic'):
            head += '   [%s]' % res['phonetic']
        segments.append((head + '\n', 'title'))

        if res.get('suggestions') and not fixed:
            segments.append(('Возможно, вы имели в виду: ', 'muted'))
            for i, s in enumerate(res['suggestions']):
                if i:
                    segments.append((' · ', 'muted'))
                self._add_link(segments, s)

        blocks = res.get('blocks', [])
        pos_counts = {}
        for b in blocks:
            key = b.get('pos') or 'значение'
            pos_counts[key] = pos_counts.get(key, 0) + 1
        seen = {}
        for block in blocks:
            pos = block.get('pos') or 'значение'
            seen[pos] = seen.get(pos, 0) + 1
            label = pos if pos_counts[pos] == 1 else '%s · %d' % (pos, seen[pos])
            segments.append((label + '\n', 'pos'))
            for i, d in enumerate(block.get('defs', []), 1):
                segments.append(('%d. %s\n' % (i, d['text']), 'def'))
                if d.get('example'):
                    segments.append(('пример: %s\n' % d['example'], 'ex'))
        if res.get('note'):
            segments.append((res['note'] + '\n', 'muted'))
        segments.append(('Источник: %s\n' % res.get('source', '—'), 'muted'))
        self._render(segments, format_text(res))

    # ------------------------------ прочее ---------------------------------
    def set_notice(self, text):
        """Показать предупреждение в панели результата (например, про хоткей)."""
        self.status.configure(text='Горячая клавиша недоступна', foreground=COLORS['fix'])
        self._render([('⚠️  %s\n\nОкно открыто; чтобы вернуть его после закрытия, '
                       'закройте и запустите приложение снова.\n' % text, 'err')], text)

    def copy_result(self):
        if not self.last_plain:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(self.last_plain)
        self.status.configure(text='Скопировано в буфер 📋', foreground=COLORS['accent'])

    def run(self):
        self._poll()
        self.root.mainloop()


def dpi_scale():
    """Масштаб экрана (для HiDPI-мониторов)."""
    if sys.platform != 'win32':
        return 1.0
    try:
        import ctypes
        dpi = ctypes.windll.user32.GetDpiForSystem()
        if dpi and dpi > 96:
            return dpi / 96.0
    except Exception:
        pass
    return 1.0


def register_hotkey(app, cfg):
    """Глобальная горячая клавиша через пакет keyboard.

    Возвращает текст предупреждения, если хоткей назначить не удалось, иначе None.
    """
    try:
        import keyboard
    except ImportError:
        msg = ('Пакет keyboard не установлен — горячая клавиша не работает.\n'
               'Установите его командой:   pip install keyboard   и перезапустите приложение.')
        print('Словарик: ' + msg)
        return msg
    try:
        keyboard.add_hotkey(cfg['hotkey'], lambda: app.jobs.put(('toggle', None)),
                            suppress=bool(cfg.get('suppress_hotkey', False)))
    except Exception as exc:
        log_exception()
        msg = ('Не удалось назначить горячую клавишу «%s»: %s\n'
               'Проверьте значение "hotkey" в файле slovarik.json.' % (cfg['hotkey'], exc))
        print('Словарик: ' + msg)
        return msg
    return None


def run_app():
    global tk
    if sys.platform == 'win32':
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    import tkinter
    tk = tkinter

    cfg = load_config()
    app = SlovarikApp(cfg)
    try:  # чтобы буквы и отступы были пропорциональны масштабу экрана
        app.root.tk.call('tk', 'scaling', dpi_scale() * 96.0 / 72.0)
    except Exception:
        pass
    warning = register_hotkey(app, cfg)
    app.show()
    if warning:
        app.set_notice(warning)
    app.run()


def main(argv):
    args = argv[1:]
    if '--version' in args:
        print('%s %s' % (APP_NAME, APP_VERSION))
        return
    if '--cli' in args:
        rest = [a for a in args if not a.startswith('--')]
        word = ' '.join(rest) if rest else ''
        if not word:
            print('Использование: python slovarik.py --cli слово [--json] [--no-fix]')
            return
        run_cli(word, as_json='--json' in args, correct='--no-fix' not in args)
        return
    run_app()


if __name__ == '__main__':
    try:
        if sys.stdout is not None and hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    main(sys.argv)
