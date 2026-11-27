---
name: ml-training-inference
description: Rules for the ML layer of Augur - one shared compute_features for train and inference with no look-ahead, triple-barrier labels, purged walk-forward with embargo, calibrated LightGBM multiclass classifier, deterministic rule-based regime_tag, model registry (ml_models lifecycle + artifacts with sha256, ml_model_version = lightgbm_v<N>), no-pickle artifacts, deterministic inference and ml_predictions with input_hash. Use when creating or modifying src/augur/fusion/ml/*, features, labeling, training, calibration, model registry, model promotion, ML inference or the ml_models / ml_predictions tables. Do NOT use for combining the ML prediction with LLM/technical signals (use hybrid-ml-llm-fusion), for LLM output (use llm-output-validation) or for sizing and orders (use auto-trade-risk-gate).
---

# ml-training-inference

ML-слой: признаки → разметка → обучение и валидация → реестр → инференс → `MLPrediction`. ML только прогнозирует. Направление сделки и `strength` задаёт `hybrid-ml-llm-fusion`, а исполнение разрешает `auto-trade-risk-gate`.

## When to use
- Создаёшь или меняешь `src/augur/fusion/ml/*`: признаки, разметку, walk-forward, обучение, калибровку, реестр, инференс.
- Меняешь таблицы `ml_models` / `ml_predictions` или CLI обучения и промоушена моделей.
- Добавляешь признак, меняешь горизонт или барьеры разметки, переобучаешь модель.

When NOT to use:
- Правила слияния ML/LLM/tech, veto, `FusedSignal` → `hybrid-ml-llm-fusion`.
- LLM-клиенты, промпты, `llm_calls` → `llm-output-validation`.
- Размер позиции, лимиты, ордера → `auto-trade-risk-gate`.
- Миграция БД, версия приложения, CHANGELOG → `release-management`.

## Объём 0.10.0
- В объёме: LightGBM multiclass `long/flat/short` с triple-barrier разметкой, калибровка и детерминированный `regime_tag` по правилам (ADX, RV(20)/RV(100), перцентиль ATR).
- Не в объёме (после 0.10.0): квантильная регрессия, модель волатильности, GARCH, HMM, TCN/LSTM. Поля `expected_return_q50` и `vol_forecast` в `MLPrediction` необязательны и не заполняются.
- Индикаторы считаются на numpy/pandas из extra `ml`, без TA-Lib и pandas-ta. Новые зависимости добавляются только с обоснованием.

## Procedure
1. Прочитай `ML_LLM_FUSION_SPEC.md` §2 (ML-слой), §6.4 (дообучение), §7 (`ml_models`, `ml_predictions`) и `VERSIONING.md` §2.3 (артефакты). В докстрингах ссылайся на разделы, не копируй спеку.
2. **Размещение и импорты.** Весь ML-код лежит в `augur.fusion.ml`. Он не импортирует `augur.execution`, `augur.risk`, `augur.adapters` (контракт import-linter для `augur.fusion`). `lightgbm`, `numpy`, `pandas` импортируются только внутри `augur.fusion.ml`. `augur.signals`, `augur.risk`, `augur.execution`, `augur.strategies` ML-библиотеки не импортируют (ARCHITECTURE.md §5).
3. **Признаки** (§2.2). Одна функция `compute_features(bars, as_of) -> FeatureVector` для обучения и инференса. Копии в скриптах и ноутбуках запрещены.
   - Используются только бары с `close_time <= as_of`, где `close_time = ts_open + timeframe_ms` (в `ohlcv` хранится `ts_open`). Бар на `as_of` должен быть закрыт.
   - Подаются только стационарные преобразования: log-returns, диапазоны, z-score, отношения. Сырые цены не подаются.
   - Rolling-окна и лаги смотрят только назад. `shift(-k)`, центрированные окна, нормализация по всей выборке запрещены.
   - Warm-up одинаковый при обучении и инференсе. Если истории не хватает, признаков нет, и инференс возвращает «ML отсутствует», а не NaN-вектор.
   - `feature_set_version = "fs_" + sha256(список фич + параметры + версия кода признаков)[:12]`. Любое изменение признака меняет хэш.
4. **Разметка** (§2.1). Triple-barrier: `+k·ATR`, `−k·ATR`, вертикальный барьер через `h` баров. Класс задаётся первым касанием, при касании вертикального барьера класс `flat`. Если оба барьера пробиты внутри одного бара, класс тоже `flat` (консервативно). `k` и `h` берутся из конфига обучения и входят в метаданные артефакта. Последние `h` баров без полной разметки в обучение не идут.
5. **Валидация** (§2.3). Только purged walk-forward с embargo: purge `h` баров между train/calib/test, embargo ≥ `max(h, 1 % длины)`. Случайный K-fold и `shuffle=True` запрещены. Гиперпараметры подбираются только внутри train (nested walk-forward). Число испытаний пишется в метрики, чтобы считать deflated Sharpe.
6. **Обучение.** `seed` фиксирован, `deterministic=True`, `force_row_wise=True`, `num_threads=1`. Scaler и любые fit-преобразования обучаются только на train фолда. Торговая метрика walk-forward считается с издержками из общей `augur.backtest.costs.CostModel`, а не из отдельной копии.
7. **Калибровка.** На калибровочном фолде: isotonic при ≥ 1000 наблюдений, иначе Platt. Параметры калибратора хранятся в JSON (точки isotonic, коэффициенты Platt) и применяются собственным кодом, без pickle sklearn-объектов. После калибровки вероятности нормируются к сумме 1. `confidence = P(direction)` после калибровки. Без калибратора и метрик `log-loss`, `brier`, `ece`, `sharpe`, `max_dd` `register()` отказывает.
8. **Артефакт** (VERSIONING §2.3, FUSION SPEC §2.6). Хранится в `artifacts/ml/lightgbm_v<N>/`: `model.txt` (нативный формат LightGBM), `calibrator.json`, `meta.json` (признаки, `feature_set_version`, `k`, `h`, `train_range`, git sha кода обучения, версии библиотек, метрики). Pickle, joblib и `torch.load` запрещены. `sha256` считается по детерминированно упорядоченному содержимому каталога. Артефакт неизменяем: любое изменение = `lightgbm_v<N+1>`. Удалять зарегистрированный артефакт запрещено.
9. **Реестр.** Две таблицы, одна версия:
   - `ml_model_version = "lightgbm_v<N>"`. `N` монотонен глобально. Эта строка пишется в `artifacts(kind='ml_model')`, `ml_models.version` и `decision_audit.ml_model_version`.
   - `ml_models` хранит жизненный цикл: `name = signal_clf_<asset>_<tf>`, `status` candidate → staging → production → retired, `approved_by`, `metrics_json`. Production-модель на `name` ровно одна (частичный UNIQUE-индекс).
   - Новая модель всегда регистрируется как `candidate`. Автопромоушен запрещён. Промоушен в `production` — только через CLI с `--confirm` и `approved_by`, по критериям §2.4: Sharpe после издержек не хуже текущей модели, ECE ≤ порога из конфига, ≥ 14 дней в `staging`. Каждая смена статуса пишется в `audit_log`.
   - Схема меняется только миграцией с записью в `schema_migrations` (`release-management`).
10. **Инференс.** Загрузка только через `ModelRegistry.get_production(name)` → `load(rec)`. `load` сверяет `sha256` с реестром и `feature_set_version` с текущим кодом признаков. При несовпадении ML считается недоступным: пишется WARNING в `audit_log`, и Fusion переходит к правилу «ML отсутствует → NO_SIGNAL». Загрузка по пути в обход реестра запрещена. Инференс выполняется на закрытии бара, `num_threads=1`.
11. **Выход.** `MLPrediction` — Pydantic-модель по §2.5, `frozen=True, extra="forbid"`: `prediction_id`, `asset`, `timestamp` (= `close_time`), `direction`, `confidence`, `class_probs`, `horizon`, `regime_tag`, `model_version`, `feature_set_version`, `input_hash = "sha256:" + hash(канонический вектор фич)`. Каждый прогноз пишется в `ml_predictions` до передачи в Fusion, `UNIQUE (model_id, asset, timestamp)` обеспечивает идемпотентность. ML не выставляет `action`, `strength` и размер позиции.
12. **Режим.** `regime_tag` из `trend_up | trend_down | range | high_vol | chaotic` вычисляется детерминированными правилами с порогами из конфига, а не моделью. Порядок правил: `chaotic` → `high_vol` → тренд → `range`.
13. **Тесты.** Запусти `pytest tests/fusion` (ML-тесты — в `tests/fusion/ml/`). Обязательны:
    - look-ahead: дописать будущие бары и пересчитать признаки на `as_of` → результат совпадает бит-в-бит;
    - walk-forward: индексы train, calib и test не пересекаются, purge и embargo соблюдены;
    - triple-barrier на ручных golden-кейсах, включая одновременное касание и вертикальный барьер;
    - детерминизм: два обучения на одних данных дают одинаковый `sha256`, два инференса — одинаковые `class_probs` и `input_hash`;
    - реестр: `load` с подменённым артефактом отказывает, вторая production-модель на `name` невозможна, без калибратора `register()` отказывает;
    - replay: прогноз из `ml_predictions` воспроизводится по `model_version` и барам.
    Тесты не требуют `lightgbm`: для них есть маленькие синтетические данные и `pytest.importorskip` там, где модель обучается на самом деле.

## Examples
```python
# src/augur/fusion/ml/features.py — единственная функция признаков (FUSION SPEC §2.2)
def compute_features(bars: pd.DataFrame, as_of: int, cfg: FeatureConfig) -> FeatureVector | None:
    closed = bars[bars["ts_open"] + cfg.timeframe_ms <= as_of]      # только закрытые бары
    if len(closed) < cfg.warmup_bars:
        return None                                               # ML отсутствует, не NaN
    logc = np.log(closed["close"].to_numpy())
    feats = {f"ret_{n}": logc[-1] - logc[-1 - n] for n in cfg.return_lags}
    ...
    return FeatureVector(as_of=as_of, values=feats, feature_set_version=cfg.feature_set_version)
```

```python
# tests/fusion/ml/test_lookahead.py — обязательный автотест (FUSION SPEC §2.3)
def test_future_bars_do_not_change_features(bars, cfg):
    as_of = close_time(bars.iloc[300], cfg)
    base = compute_features(bars.iloc[:301], as_of, cfg)
    with_future = compute_features(bars, as_of, cfg)              # +N будущих баров
    assert base == with_future
```

```python
# Инференс: только production из реестра, со сверкой хэшей
rec = registry.get_production("signal_clf_btcusdt_15m")
predictor = registry.load(rec)            # sha256 + feature_set_version, иначе ModelUnavailable
pred = predictor.predict(compute_features(bars, as_of, cfg))
repo.insert_prediction(pred)              # до передачи в fuse()
```

## Anti-patterns
- **Отдельный пайплайн признаков для обучения и инференса.** Training-serving skew: модель в проде видит другие числа, чем в бэктесте.
- **`shift(-1)`, центрированные окна, scaler на всей выборке.** Look-ahead даёт отличный бэктест и ноль или минус в проде.
- **Случайный K-fold или подбор гиперпараметров по test.** Утечка через автокорреляцию и переобучение на тест.
- **Сырой `predict_proba` вместо калиброванного.** Порог `ml_min_confidence` из `FusionConfig` теряет смысл.
- **`model.pkl` в корне или загрузка по пути.** Решение нельзя воспроизвести, а pickle исполняет произвольный код.
- **Перезапись артефакта под той же версией.** Старые ордера в `decision_audit` начинают ссылаться на другую модель.
- **ML выставляет `action` или размер позиции.** ML только прогнозирует. Действие решает Fusion, объём — Risk Gate.
- **Обучение на собственных сделках.** Selection bias фильтра: метки берутся только из полной истории баров (§6.4).
- **Автопромоушен в production.** Модель без shadow-периода и ручного подтверждения начинает торговать.
