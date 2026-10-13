# -*- coding: utf-8 -*-
"""Автотесты разбора вики-разметки. Работают без интернета.

Запуск из папки проекта:
    python -m unittest discover -s tests -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import slovarik as sl  # noqa: E402


class TestTemplates(unittest.TestCase):
    """Шаблоны, из-за которых раньше терялись толкования."""

    def test_word_template(self):
        # {{===|онанист}} — так в статье «дрочила» оформлено само толкование
        self.assertEqual(sl.handle_template('===|онанист'), 'онанист')

    def test_term_template_single(self):
        self.assertEqual(sl.handle_template('t:=|даже'), 'даже')

    def test_term_template_with_gloss(self):
        # ссылки [[...]] раскрываются уже на этапе clean_wiki
        self.assertEqual(sl.clean_wiki('{{t:=|сторожевой таймер|[[таймер]]}}'),
                         'сторожевой таймер — таймер.')

    def test_context_marks(self):
        for inner, expected in [('разг.|ru', '(разг.)'), ('груб.|ru', '(груб.)'),
                                ('бранн.|ru', '(бран.)'), ('несов.|ru', '(несов.)'),
                                ('мол.|ru', '(мол.)'), ('зоол.|ru', '(зоол.)')]:
            with self.subTest(inner=inner):
                self.assertEqual(sl.handle_template(inner), expected)

    def test_compound_mark(self):
        self.assertEqual(sl.handle_template('техн.жарг.|ru'), '(техн. жарг.)')
        self.assertEqual(sl.handle_template('др.-рус.|-'), '(др.-рус.)')

    def test_symbols(self):
        self.assertEqual(sl.handle_template('-'), '-')
        self.assertEqual(sl.handle_template('--'), '—')
        self.assertEqual(sl.handle_template('!'), '|')
        self.assertEqual(sl.handle_template('итп'), ' и т. п.')
        self.assertEqual(sl.handle_template('"|Друг'), '«Друг»')

    def test_dropped_templates(self):
        for inner in ('пример|текст', 'семантика|синонимы=-', 'морфо-ru|дроч',
                      'транскрипция|слово', 'improve|ru|', 'омонимы|ru|4'):
            with self.subTest(inner=inner):
                self.assertEqual(sl.handle_template(inner), '')

    def test_text_is_kept(self):
        self.assertEqual(sl.handle_template('выдел|дрочила'), 'дрочила')
        self.assertEqual(sl.handle_template('lang|la|felidae'), 'felidae')


class TestSections(unittest.TestCase):
    def test_language_section_with_parameter(self):
        # заголовок бывает с параметром: «= {{-ru-|nocat}} =»
        self.assertEqual(sl.section_lang('{{-ru-|nocat}}'), '-ru-')
        self.assertEqual(sl.section_lang('{{-ru-}}'), '-ru-')
        self.assertEqual(sl.section_lang('Русский'), 'русский')

    def test_find_ru_section(self):
        text = "= {{-en-}} =\nанглийское\n= {{-ru-|nocat}} =\nрусское\n"
        self.assertEqual(sl.find_ru_section(text).strip(), 'русское')


DROCHILA = """{{offensive}}
= {{-ru-}} =
== {{з|(существительное)}} ==
=== Морфологические и синтаксические свойства ===
{{сущ-ru|дрочи́ла|мо <жо 1a>|слоги={{по-слогам|дро|чи́|ла}}}}
=== Семантические свойства ===
==== Значение ====
# {{разг.|ru}}, {{груб.|ru}} {{===|онанист}} {{пример|Он был {{выдел|дрочила}}.}}
# 
== {{з|(глагол I)}} ==
* {{Форма-гл
|база=дрочить#I
|время=п
|род=ж
|число=ед
|накл=и
|помета={{диал.|-}}
|язык=ru
|слоги={{по-слогам|дро|чи́|ла}}
}}
== {{з|(глагол II)}} ==
* {{Форма-гл|база=дрочить#II|время=п|род=ж|число=ед|накл=и|помета={{вульг.|-}}|знач=заниматься онанизмом|язык=ru}}
"""

IDET = """= {{-ru-|nocat}} =
{{Форма-гл
|база=идти
|время=наст
|род=
|лицо=3
|число=ед
|накл=
|помета=
|знач=
|язык=ru
}} {{пример|Сильнейший меня {{выдел|идёт}}.}}
"""

KOSA = """= {{-ru-}} =
== {{з|(существительное)}} ==
=== Морфологические и синтаксические свойства ===
{{сущ-ru|коса́}}
=== Семантические свойства ===
==== Значение ====
# вид укладки волос
== {{з|(форма)}} ==
* {{Форма-сущ|кос#I|в|мн|знач=скворец}}
"""


class TestRussianArticle(unittest.TestCase):
    def test_drochila(self):
        """Главный случай: слово-толкование спрятано в шаблоне {{===|...}}."""
        blocks = sl.parse_ru_section(DROCHILA, 5)
        self.assertTrue(blocks, 'должен быть хотя бы один блок значений')
        first = blocks[0]
        self.assertEqual(first['pos'], 'существительное')
        self.assertIn('онанист', first['defs'][0]['text'])
        self.assertIn('(разг.)', first['defs'][0]['text'])

    def test_drochila_forms(self):
        """Разделы «форма глагола» должны попадать в результат."""
        blocks = sl.parse_ru_section(DROCHILA, 5)
        form_blocks = [b for b in blocks if b['pos'] == 'форма глагола']
        self.assertEqual(len(form_blocks), 1, 'формы одного вида объединяются в блок')
        defs = form_blocks[0]['defs']
        self.assertEqual(len(defs), 2)
        self.assertIn('прошедшего времени', defs[0]['text'])
        self.assertIn('дрочить', defs[0]['text'])
        self.assertIn('(диал.)', defs[0]['text'])
        self.assertIn('заниматься онанизмом', defs[1]['text'])

    def test_word_form_article(self):
        """Статья-форма: «идёт» — заголовок с параметром и только {{Форма-гл}}."""
        blocks = sl.parse_ru_section(IDET, 5)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]['pos'], 'форма глагола')
        text = blocks[0]['defs'][0]['text']
        self.assertIn('настоящего времени', text)
        self.assertIn('идти', text)

    def test_noun_form(self):
        blocks = sl.parse_ru_section(KOSA, 5)
        labels = [b['pos'] for b in blocks]
        self.assertIn('существительное', labels)
        self.assertIn('форма существительного', labels)
        form = next(b for b in blocks if b['pos'] == 'форма существительного')
        text = form['defs'][0]['text']
        self.assertIn('винительного падежа', text)
        self.assertIn('множественного числа', text)
        self.assertIn('кос', text)
        self.assertIn('скворец', text)

    def test_mark_only_lines_are_skipped(self):
        """«(разг.), (груб.).» без толкования показывать нельзя."""
        text = ("= {{-ru-}} =\n== {{з|(существительное)}} ==\n"
                "==== Значение ====\n"
                "# {{разг.|ru}}, {{груб.|ru}}\n"
                "# настоящий смысл\n")
        blocks = sl.parse_ru_section(text, 5)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(len(blocks[0]['defs']), 1)
        self.assertEqual(blocks[0]['defs'][0]['text'], 'настоящий смысл.')

    def test_pos_from_homonym(self):
        self.assertEqual(sl.pos_from_homonym('{{з|(наречие I)}}'), 'наречие')
        self.assertEqual(sl.pos_from_homonym('{{з|(существительное)}}'), 'существительное')


if __name__ == '__main__':
    unittest.main(verbosity=2)
