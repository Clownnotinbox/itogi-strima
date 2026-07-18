import unittest

import pipeline


class LiveQuoteQualityTests(unittest.TestCase):
    def test_candidate_text_is_normalized_without_topic_specific_rewrites(self):
        cases = {
            "  то есть иногда чай остывает раньше разговора  ": (
                "Иногда чай остывает раньше разговора"
            ),
            "мы  снова   опоздали к началу": "Мы снова опоздали к началу",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(pipeline._normalize_live_candidate_text(source), expected)

    def test_llm_fix_must_be_a_small_edit(self):
        original = "Кажется дождь начался неожиданно"
        small_fix = "Кажется, дождь начался неожиданно"
        rewrite = "Погода резко испортилась и всем пришлось вернуться домой"

        self.assertEqual(pipeline._safe_live_fix(original, small_fix), small_fix)
        self.assertEqual(pipeline._safe_live_fix(original, rewrite), "")

    def test_context_extension_must_be_a_verbatim_continuous_span(self):
        original = "Проектор снова завис"
        context = "Проверим кабель. Проектор снова завис. Зато есть время на чай."
        expanded = "Проектор снова завис. Зато есть время на чай"
        invented = "Проектор завис, поэтому мы решили пить чай"

        self.assertEqual(
            pipeline._safe_live_context_quote(original, expanded, context),
            expanded,
        )
        self.assertEqual(pipeline._safe_live_context_quote(original, invented, context), "")

    def test_rerank_dedupe_prefers_complete_extension_over_old_score(self):
        candidates = [
            {"text": "Проектор снова завис", "score": 90},
            {"text": "Проектор снова завис. Зато есть время на чай.", "score": 56},
        ]

        result = pipeline._dedupe_live_for_rerank(candidates)

        self.assertEqual(len(result), 1)
        self.assertIn("время на чай", result[0]["text"])

    def test_final_gate_rejects_fragments_but_keeps_complete_thoughts(self):
        self.assertFalse(pipeline._live_quote_passes_final_gate("если завтра будет, то"))
        self.assertFalse(pipeline._live_quote_passes_final_gate("да, в комнате, всегда"))
        self.assertTrue(
            pipeline._live_quote_passes_final_gate(
                "Я пришёл отдыхать, но теперь устал от отдыха."
            )
        )
        self.assertTrue(
            pipeline._live_quote_passes_final_gate(
                "Зачем покупать будильник, если я всё равно просыпаюсь раньше?"
            )
        )

    def test_technical_commands_are_dropped_and_context_is_attached(self):
        segments = [
            {"text": "Так, секунду, сейчас открою настройки.", "start": 0, "end": 2},
            {"text": "Я пришёл отдыхать, но теперь устал от отдыха.", "start": 30, "end": 33},
            {"text": "Включите музыку, давайте.", "start": 60, "end": 62},
            {
                "text": "После каждого промаха буду комментировать матч шёпотом.",
                "start": 90,
                "end": 93,
            },
        ]

        quotes = pipeline.quick_live_quotes(segments, limit=20)
        texts = {quote["text"] for quote in quotes}

        self.assertIn("Я пришёл отдыхать, но теперь устал от отдыха", texts)
        self.assertIn("После каждого промаха буду комментировать матч шёпотом", texts)
        self.assertNotIn("Включите музыку, давайте", texts)

        contrast = next(q for q in quotes if "устал от отдыха" in q["text"])
        self.assertIn("настройки", contrast["context"])
        self.assertIn("музыку", contrast["context"])

    def test_playful_promise_beats_incomplete_live_fragments(self):
        segments = [
            {
                "text": "После каждого промаха буду комментировать матч шёпотом.",
                "start": 0,
                "end": 2,
            },
            {"text": "Уже потом даже снова даже.", "start": 30, "end": 32},
            {"text": "Есть люди, у которых каждый.", "start": 60, "end": 62},
            {"text": "Никто пока не знает.", "start": 90, "end": 92},
        ]

        quotes = pipeline.quick_live_quotes(segments, limit=20)
        texts = {quote["text"] for quote in quotes}

        self.assertIn("После каждого промаха буду комментировать матч шёпотом", texts)
        self.assertNotIn("Уже потом даже снова даже", texts)
        self.assertNotIn("Есть люди, у которых каждый", texts)

    def test_neighbor_segments_and_long_clauses_create_complete_candidates(self):
        segments = [
            {"text": "Проектор снова завис.", "start": 0, "end": 2},
            {"text": "Зато есть время на чай.", "start": 2.3, "end": 3.5},
            {
                "text": (
                    "Зачем называть это быстрым решением, если подготовка занимает весь день, "
                    "это не экономит время, это просто переносит работу на вечер"
                ),
                "start": 10,
                "end": 18,
            },
        ]

        quotes = pipeline.quick_live_quotes(segments, limit=30)
        texts = {quote["text"] for quote in quotes}

        self.assertTrue(
            any("Проектор снова завис" in text and "время на чай" in text for text in texts),
            texts,
        )
        self.assertTrue(
            any("не экономит время" in text and "работу на вечер" in text for text in texts),
            texts,
        )

    def test_question_can_join_a_later_contrast_without_topic_specific_rules(self):
        segments = [
            {"text": "Зачем осуждать людей за дорогие покупки?", "start": 0, "end": 2},
            {"text": "Это вообще странно.", "start": 2.2, "end": 3.2},
            {"text": "Хотя, если бы мне предложили спортивную машину,", "start": 3.4, "end": 5.5},
            {"text": "я бы сама сразу согласилась.", "start": 5.6, "end": 7.0},
        ]

        quotes = pipeline.quick_live_quotes(segments, limit=30)
        texts = {quote["text"] for quote in quotes}

        self.assertTrue(
            any("Зачем осуждать" in text and "сама сразу согласилась" in text for text in texts),
            texts,
        )

    def test_emotional_rhetorical_question_gets_generic_signal(self):
        text = "Почему будильники существуют в выходные, мне интересно?"
        sig = pipeline.F.signals(text)
        features = pipeline._live_features(text, sig, 0.0)

        self.assertEqual(features["sharp_question"], 1)
        self.assertEqual(pipeline._live_reason(features, []), "резкий риторический вопрос")

    def test_complete_generic_quotes_can_be_rescored_after_context_edit(self):
        contrast = pipeline.score_live_quote_text(
            "Зачем осуждать людей за дорогие покупки? Хотя, если бы мне предложили машину, "
            "я бы сама сразу согласилась."
        )
        rhetorical = pipeline.score_live_quote_text(
            "Почему будильники существуют в выходные, мне интересно?"
        )

        self.assertIsNotNone(contrast)
        self.assertIsNotNone(rhetorical)
        self.assertGreaterEqual(contrast["score"], 60)
        self.assertGreaterEqual(rhetorical["score"], 70)


if __name__ == "__main__":
    unittest.main()
