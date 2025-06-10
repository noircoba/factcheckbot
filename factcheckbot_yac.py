# Импортируем библиотеки
import logging # Для записи логов работы программы (помогает отслеживать ошибки и события)
from telegram import Update # Базовый класс для обработки входящих сообщений
from telegram.ext import Application, MessageHandler, filters, ContextTypes # Обработка событий в Telegram
from telegram.helpers import escape_markdown # Экранирование текста для Telegram-сообщений
import requests # HTTP-запросы к API
import json # Работа с JSON-данными
import ollama # Использование LLM (Large Language Model)
from bs4 import BeautifulSoup # Парсинг HTML/XML документов
from bs4 import XMLParsedAsHTMLWarning # Предупреждения от библиотеки BeautifulSoup
import warnings # Управление предупреждениями
import asyncio # Асинхронная обработка запросов
from urllib.parse import urlparse # Парсинг URL
import time # Временные задержки и измерение времени
from random import uniform # Генерация случайных чисел для избежания флуда
import re # Регулярные выражения
from collections import defaultdict # Для работы с пользовательскими ограничениями
from datetime import datetime, timedelta # Работа с датой и временем

# Конфигурация (заполнить своими данными)
from config import (
    TELEGRAM_TOKEN, 
    YANDEX_USER,
    YANDEX_API_KEY,
    YANDEX_FOLDER_ID,
    YANDEX_SEARCH_URL
) # Конфигурационные параметры для Telegram и Yandex Search API

# Инициализация Ollama
ollama.pull('yandex/YandexGPT-5-Lite-8B-instruct-GGUF') # Загрузка модели LLM для анализа фактов

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
) # Форматирование и уровень логов для отслеживания работы программы
logger = logging.getLogger(__name__) # Логгер для текущего модуля

# Отключаем предупреждения XMLParsedAsHTMLWarning
warnings.filterwarnings('ignore', category=XMLParsedAsHTMLWarning) # Игнорирование предупреждений при парсинге HTML

# Класс для контроля флуда
class FloodControl:
    def __init__(self, max_requests_per_hour=15):
        self.max_requests = max_requests_per_hour
        self.user_requests = defaultdict(list)  # {user_id: [timestamp1, timestamp2, ...]}
    
    def check_user(self, user_id):
        """Проверяет, может ли пользователь сделать запрос"""
        now = datetime.now()
        user_requests = self.user_requests[user_id]
        
        # Очищаем старые запросы (старше часа)
        user_requests[:] = [req_time for req_time in user_requests 
                           if now - req_time < timedelta(hours=1)]
        
        # Проверяем лимит
        if len(user_requests) >= self.max_requests:
            return False
        
        # Добавляем текущий запрос
        user_requests.append(now)
        return True
    
    def get_remaining_requests(self, user_id):
        """Возвращает количество оставшихся запросов"""
        now = datetime.now()
        user_requests = self.user_requests[user_id]
        
        # Очищаем старые запросы
        user_requests[:] = [req_time for req_time in user_requests 
                           if now - req_time < timedelta(hours=1)]
        
        return max(0, self.max_requests - len(user_requests))

# Глобальный экземпляр контроля флуда
flood_control = FloodControl(max_requests_per_hour=15)

def handle_api_error(code: str, message: str) -> list:
    """Обработка специфичных ошибок API"""
    error_map = {
        '42': 'Неверные аутентификационные данные',
        '32': 'Превышена квота запросов',
        '55': 'Превышено RPS-ограничение',
        '15': 'Нет результатов поиска'
    } # Сопоставление кодов ошибок с описаниями
    
    return [{
        'title': error_map.get(code, 'Неизвестная ошибка API'),
        'url': 'https://yandex.cloud/ru/docs/search-api/reference/error-codes',
        'snippet': f'Код {code}: {message}'
    }] # Возвращаем список с ошибкой и ссылкой на документацию

async def analyze_facts(text: str) -> dict:
    """Извлечение проверяемых фактов из текста с максимальным контекстом"""
    prompt = f"""
Проанализируй новостной текст и выдели из него проверяемые факты для дальнейшей верификации.
ТОЛЬКО факты, которые НЕПОСРЕДСТВЕННО относятся к основной теме новости.

ТРЕБОВАНИЯ К ФАКТАМ:
1. Каждый факт должен содержать ПОЛНЫЙ контекст:
   - ЧТО конкретно произошло (точное событие/заявление/действие)
   - ГДЕ это произошло (точная географическая привязка)
   - КОГДА это произошло (точная дата и время, если указаны)
   - КТО участвовал/сообщил/заявил (конкретные имена, должности, организации)
   - ДОПОЛНИТЕЛЬНЫЕ ДЕТАЛИ (цифры, суммы, количества, условия)

2. Включай временные маркеры: "15 ноября 2024 года", "в 14:30 по московскому времени", "вчера", "на прошлой неделе"

3. Сохраняй все числовые данные, имена собственные, названия организаций, географические названия

4. Формулируй факты как САМОСТОЯТЕЛЬНЫЕ предложения, понятные без контекста исходной статьи

5. НЕ включай:
   - Оценочные суждения и мнения
   - Общие рассуждения
   - Факты, не относящиеся к основной теме новости
   - Второстепенную информацию

6. Собери полный контекст новости как то: ЧТО, ГДЕ, КОГДА, КТО и ДОПОЛНИТЕЛЬНЫЕ ДЕТАЛИ в отдельную строку и используй ее для добавления в каждый факт

7. Проверь, что выделенные факты содержат полный контекст как то: ЧТО, ГДЕ, КОГДА, КТО и ДОПОЛНИТЕЛЬНЫЕ ДЕТАЛИ, если нет то обогати факт ими

ПРИМЕР НЕПРАВИЛЬНОГО извлечения (мало контекста):
"Произошло землетрясение"
"Землетрясение было сильным"

ПРИМЕР ПРАВИЛЬНОГО извлечения (полный контекст):
"15 ноября 2024 года в 11:27 по московскому времени произошло землетрясение магнитудой 4.2 балла в районе водопада Учан-Су в Крыму, по данным замдиректора Института сейсмологии и геодинамики Марины Бондарь"
"Эпицентр землетрясения 15 ноября 2024 года находился на глубине 10 километров под землёй в районе водопада Учан-Су в Крыму, согласно данным сейсмологической службы"

Верни ТОЛЬКО JSON без пояснений:
{{
    "facts": ["полный факт 1 с максимальным контекстом", "полный факт 2 с максимальным контекстом", ...]
}}

Текст: {text[:2500]}
"""
    logger.info(f"LLM Fact Extraction: {text[:350]!r}") # Логирование входного запроса
    try:
        resp = ollama.generate(
            model='yandex/YandexGPT-5-Lite-8B-instruct-GGUF',
            prompt=prompt,
            format='json',
            options={'temperature': 0.1, 'num_ctx': 16384}
        ) # Вызов LLM с настройками
        raw = resp['response'].strip().replace('```json', '').replace('```', '') # Удаление лишних символов JSON
        try:
            return json.loads(raw) # Парсинг JSON-ответа
        except json.JSONDecodeError:
            import json_repair # Использование библиотеки для исправления JSON
            return json.loads(json_repair.repair_json(raw)) # Попытка исправить JSON
    except Exception as err:
        logger.error(f"Ошибка analyze_facts: {err}") # Логирование ошибок
        return {"facts": []} # Возврат пустого списка фактов при ошибках

async def yandex_factcheck(fact: str) -> list:
    """Поиск подтверждающих источников через Yandex Search API"""
    try:
        original_fact = fact
        logger.info(f"Поиск источников для факта: '{original_fact}'") # Логирование исходного факта
        
        # Формируем XML запрос согласно документации
        request_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
        <request>
            <query>{original_fact}</query>
            <page>0</page>
            <sortby order="descending">rlv</sortby>
            <maxpassages>3</maxpassages>
            <groupings>
                <groupby attr="d" mode="deep" groups-on-page="10" docs-in-group="1"/>
            </groupings>
        </request>
        """
        
        # Актуальные параметры аутентификации
        params = {
            'folderid': YANDEX_FOLDER_ID,  # Идентификатор каталога
            'apikey': YANDEX_API_KEY,      # API-ключ сервисного аккаунта
            'type': 'xml'                  # Формат ответа
        }
        
        headers = {
            'Content-Type': 'application/xml',
            'Accept': 'application/xml'
        }

        # Используем актуальный API endpoint
        api_url = 'https://yandex.ru/search/xml' # URL для запросов
        
        logger.info(f"Отправляем запрос к Yandex Search API: {original_fact[:150]}...") # Логирование отправки запроса
        await asyncio.sleep(uniform(0.7, 1.2)) # Рандомная задержка для избежания флуда
        
        response = requests.post(
            api_url,
            params=params,
            data=request_xml.encode('utf-8'),
            headers=headers,
            timeout=15
        ) # Отправка запроса к API
        
        response.raise_for_status() # Проверка на ошибки HTTP
        
        # Обработка ответа
        xml_soup = BeautifulSoup(response.text, 'xml') # Парсинг XML
        
        # Обработка ошибок API
        if error := xml_soup.find('error'):
            error_code = error.get('code', 'unknown')
            logger.error(f"Ошибка API (код {error_code}): {error.text}") # Логирование ошибок API
            return handle_api_error(error_code, error.text) # Возврат обработанной ошибки
        
        # Извлечение результатов
        results = []
        for doc in xml_soup.find_all('doc'):
            try:
                url = doc.find('url').text.strip()
                title = doc.find('title').text.strip() if doc.find('title') else "Без заголовка"
                snippet = ' '.join([p.text for p in doc.find_all('passage')][:3]) # Больше отрывков
        
                results.append({
                    'title': title[:250],
                    'url': url,
                    'snippet': snippet[:500]  # Размер отрывка
                })
            except Exception as doc_err:
                logger.warning(f"Ошибка обработки документа: {doc_err}") # Логирование ошибок при обработке документов

        return results if results else [{
            'title': 'Информация не найдена',
            'url': '',
            'snippet': f'По запросу "{original_fact}" ничего не найдено'
        }] # Возврат результата или сообщения о неудаче
        
    except Exception as err:
        logger.error(f"Критическая ошибка: {err}", exc_info=True) # Логирование критических ошибок
        return [{
            'title': 'Ошибка системы',
            'url': '',
            'snippet': 'Временные технические неполадки. Попробуйте позже'
        }] # Возврат сообщения о системной ошибке

async def analyze_news_text(text: str) -> dict:
    """Анализ текста новости на предмет достоверности и качества"""
    truncated_text = text[:3000] + ("..." if len(text) > 3000 else "") # Обрезка длинного текста
    
    prompt = f"""
Проанализируй текст новости по внутренним признакам достоверности и качества журналистики:

КРИТЕРИИ АНАЛИЗА:
1. СТИЛЬ И ЯЗЫК:
   - Нейтральный/объективный vs эмоционально окрашенный/манипулятивный
   - Использование фактов vs оценочных суждений
   - Профессиональность изложения

2. ВНУТРЕННЯЯ СОГЛАСОВАННОСТЬ:
   - Логические противоречия внутри текста
   - Согласованность временных рамок
   - Согласованность фактических утверждений

3. КОНКРЕТНОСТЬ И ДЕТАЛИЗАЦИЯ:
   - Наличие точных дат, времени, мест
   - Конкретные имена, должности, организации
   - Точные цифры и статистика
   - Конкретные источники информации

4. ИСТОЧНИКИ И ССЫЛКИ:
   - Упоминание источников информации
   - Авторитетность упомянутых источников
   - Прямые цитаты vs пересказ

5. БАЛАНС И ОБЪЕКТИВНОСТЬ:
   - Представление разных точек зрения
   - Избегание односторонности
   - Разделение фактов и мнений

6. ПРИЗНАКИ ДЕЗИНФОРМАЦИИ:
   - Сенсационность заголовков
   - Категоричные утверждения без доказательств
   - Эмоциональное воздействие вместо фактов
   - Отсутствие контекста

Верни результат в JSON:
{{
  "credibility_score": число от 0 до 100,
  "style_analysis": "оценка стиля и языка",
  "logical_consistency": "оценка внутренней логики",
  "specificity_level": "уровень конкретности", 
  "sources_quality": "анализ упомянутых источников",
  "balance_assessment": "оценка баланса и объективности",
  "manipulation_signs": "обнаруженные признаки манипуляции",
  "strong_points": ["список сильных сторон текста"],
  "weak_points": ["список слабых мест"],
  "overall_conclusion": "общий вывод о качестве и надежности"
}}

Текст новости: {truncated_text}
"""
    try:
        resp = ollama.generate(
            model='yandex/YandexGPT-5-Lite-8B-instruct-GGUF',
            prompt=prompt,
            format='json',
            options={'temperature': 0.1, 'num_ctx': 16384}
        )
        
        raw = resp['response'].strip().replace('```json', '').replace('```', '')
        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            import json_repair
            result = json.loads(json_repair.repair_json(raw))
            
        return result
    except Exception as err:
        logger.error(f"Ошибка analyze_news_text: {err}") # Логирование ошибок
        return {
            "credibility_score": 50,
            "style_analysis": "Не удалось оценить",
            "logical_consistency": "Не удалось оценить", 
            "specificity_level": "Не удалось оценить",
            "sources_quality": "Не удалось оценить",
            "balance_assessment": "Не удалось оценить",
            "manipulation_signs": "Не удалось обнаружить",
            "strong_points": ["Анализ недоступен"],
            "weak_points": ["Анализ недоступен"],
            "overall_conclusion": "Не удалось выполнить анализ текста новости."
        } # Возврат стандартного ответа при ошибке

def remove_thinking_tags(text):
    """Удаляет содержимое между тегами <think> и </think>"""
    import re
    pattern = r'<think>.*?</think>' # Регулярное выражение для поиска маркеров мышления
    # Используем re.DOTALL, чтобы точка соответствовала также символам новой строки
    cleaned_text = re.sub(pattern, '', text, flags=re.DOTALL)
    return cleaned_text

def is_meaningful_text(text: str) -> bool:
    """Проверяет осмысленность текста"""
    clean_text = re.sub(r'\s+', ' ', re.sub(r'[^\w\s]', '', text)).strip() # Очистка текста от специальных символов
    return (
        len(clean_text) >= 16 and # Минимальная длина
        len(set(clean_text.split())) >= 4 and # Разнообразие слов
        any(c.isalpha() for c in clean_text) # Наличие букв
    )

async def perform_factchecking(user_text, facts, fact_results):
    """Проверка соответствия фактов источникам с фильтрацией нерелевантных фактов"""
    
    # Сначала фильтруем факты на релевантность к новости
    relevant_facts = await filter_relevant_facts(user_text, facts)
    
    # Подготовка данных для анализа только релевантных фактов
    factcheck_data = {
        "original_text": user_text[:3000],
        "relevant_facts": relevant_facts,
        "sources_data": {fact: fact_results[fact] for fact in relevant_facts if fact in fact_results}
    }
    
    data_str = json.dumps(factcheck_data, ensure_ascii=False)
    
    prompt = f"""
Выполни проверку фактов между текстом новости и найденными источниками.
АНАЛИЗИРУЙ ТОЛЬКО ФАКТЫ, КОТОРЫЕ НЕПОСРЕДСТВЕННО ОТНОСЯТСЯ К ОСНОВНОЙ ТЕМЕ НОВОСТИ.

Для каждого релевантного факта проведи многоуровневую проверку:

1. СООТВЕТСТВИЕ ИСТОЧНИКАМ:
   - Полностью подтверждается (факт точно совпадает с источниками)
   - Частично подтверждается (основа факта верна, но есть неточности)
   - Не подтверждается (источники не содержат подтверждения)
   - Противоречит источникам (источники опровергают факт)

2. ТОЧНОСТЬ ИЗЛОЖЕНИЯ:
   - Точно (факт изложен корректно)
   - С незначительными искажениями (мелкие неточности)
   - С существенными искажениями (значительные неточности)
   - С преувеличениями (факт раздут или драматизирован)

3. КОНТЕКСТНОСТЬ:
   - Полный контекст (вся важная информация представлена)
   - Неполный контекст (упущены важные детали)
   - Вне контекста (факт представлен без необходимого контекста)
   - Ложный контекст (факт помещен в неправильный контекст)

4. ВРЕМЕННАЯ ТОЧНОСТЬ:
   - Соответствует времени события
   - Незначительные расхождения во времени
   - Существенные временные ошибки

Верни результат в формате JSON:
{{
  "factcheck_results": [
    {{
      "fact": "проверяемый факт",
      "relevance_to_news": "высокая/средняя/низкая",
      "source_confirmation": "подтвержден/частично подтвержден/не подтвержден/противоречит",
      "accuracy_level": "точно/незначительные искажения/существенные искажения/преувеличения",
      "context_completeness": "полный/неполный/вне контекста/ложный контекст",
      "temporal_accuracy": "соответствует/незначительные расхождения/существенные ошибки",
      "source_count": число найденных источников,
      "confidence_score": число от 0 до 100,
      "explanation": "объяснение результата проверки"
    }}
  ],
  "overall_factcheck_score": число от 0 до 100,
  "overall_assessment": "общая оценка соответствия фактов источникам",
  "methodology_notes": "заметки о методологии проверки"
}}

Данные: {data_str}
"""
    
    try:
        resp = ollama.generate(
            model='yandex/YandexGPT-5-Lite-8B-instruct-GGUF',
            prompt=prompt,
            format='json',
            options={'temperature': 0.05, 'num_ctx': 16384}  # Снижена температура для большей точности
        )
        
        raw = resp['response'].strip().replace('```json', '').replace('```', '')
        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            import json_repair
            result = json.loads(json_repair.repair_json(raw))
            
        return result # Возвращение результата
    except Exception as err:
        logger.error(f"Ошибка perform_factchecking: {err}") # Логирование ошибок
        return {
            "factcheck_results": [
                {
                    "fact": "Ошибка проверки",
                    "relevance_to_news": "не определено",
                    "source_confirmation": "не определено",
                    "accuracy_level": "не определено",
                    "context_completeness": "не определено",
                    "temporal_accuracy": "не определено",
                    "source_count": 0,
                    "confidence_score": 0,
                    "explanation": "Произошла ошибка при проверке фактов"
                }
            ],
            "overall_factcheck_score": 30,
            "overall_assessment": "Не удалось выполнить полноценную проверку фактов",
            "methodology_notes": "Проверка была прервана из-за технической ошибки"
        }

async def filter_relevant_facts(text: str, facts: list) -> list:
    """Фильтрует факты, оставляя только те, которые относятся к основной теме новости"""
    
    prompt = f"""
Определи, какие из извлеченных фактов НЕПОСРЕДСТВЕННО относятся к основной теме новости.
ИСКЛЮЧИ факты, которые:
- Являются общими рассуждениями
- Относятся к побочным темам
- Не связаны с основными событиями
- Представляют собой контекстную информацию, не являющуюся ключевой для новости

Оставь только факты, которые:
- Напрямую описывают основные события новости
- Содержат ключевую информацию для понимания сути новости
- Являются проверяемыми утверждениями о конкретных фактах

Текст новости: {text[:1000]}

Факты для фильтрации:
{json.dumps(facts, ensure_ascii=False)}

Верни JSON со списком ТОЛЬКО релевантных фактов:
{{
  "relevant_facts": ["релевантный факт 1", "релевантный факт 2", ...]
}}
"""
    
    try:
        resp = ollama.generate(
            model='yandex/YandexGPT-5-Lite-8B-instruct-GGUF',
            prompt=prompt,
            format='json',
            options={'temperature': 0.1, 'num_ctx': 16384}
        )
        
        raw = resp['response'].strip().replace('```json', '').replace('```', '')
        try:
            result = json.loads(raw)
            return result.get('relevant_facts', facts)  # Возвращаем исходные факты если фильтрация не сработала
        except json.JSONDecodeError:
            import json_repair
            result = json.loads(json_repair.repair_json(raw))
            return result.get('relevant_facts', facts)
            
    except Exception as err:
        logger.error(f"Ошибка filter_relevant_facts: {err}")
        return facts  # Возвращаем исходные факты при ошибке

async def evaluate_sources_quality(fact_results: dict) -> dict:
    """Оценивает качество и надежность найденных источников с подсчетом"""
    sources_assessment = {}
    
    for fact, sources in fact_results.items():
        source_count = len(sources) if sources else 0
        
        if not sources:
            sources_assessment[fact] = {
                "reliability_score": 0,
                "sources_count": 0,
                "authoritative_sources": False,
                "consensus": "Нет данных",
                "summary": "Источники не найдены",
                "top_source": None
            }
            continue
        
        # Собираем данные для оценки качества источников
        sources_data = []
        for src in sources:
            url = src.get('url', '')
            title = src.get('title', '')
            snippet = src.get('snippet', '')
            
            sources_data.append({
                'url': url,
                'title': title,
                'snippet': snippet[:250]
            })
        
        # Оцениваем качество источников с помощью LLM
        prompt = f"""
Оцени качество и надежность источников для проверки факта: "{fact}"

Количество найденных источников: {source_count}

Данные источников:
{json.dumps(sources_data, ensure_ascii=False)}

Верни оценку в формате JSON:
{{
  "reliability_score": число от 0 до 100,
  "sources_count": {source_count},
  "authoritative_sources": true/false - есть ли авторитетные СМИ/организации,
  "consensus": "согласуются ли источники между собой",
  "summary": "краткий вывод о качестве источников",
  "top_source_index": индекс (0-{len(sources_data)-1}) самого надежного источника,
  "source_diversity": "оценка разнообразия типов источников"
}}
"""
        try:
            resp = ollama.generate(
                model='yandex/YandexGPT-5-Lite-8B-instruct-GGUF',
                prompt=prompt,
                format='json',
                options={'temperature': 0.1, 'num_ctx': 16384}
            )
            
            raw = resp['response'].strip().replace('```json', '').replace('```', '')
            try:
                assessment = json.loads(raw)
            except json.JSONDecodeError:
                import json_repair
                assessment = json.loads(json_repair.repair_json(raw))
            
            # Добавляем информацию о лучшем источнике
            top_index = assessment.get('top_source_index', 0)
            if 0 <= top_index < len(sources):
                assessment['top_source'] = sources[top_index]
            else:
                assessment['top_source'] = sources[0] if sources else None
                
            sources_assessment[fact] = assessment # Сохранение оценки
        except Exception as err:
            logger.error(f"Ошибка evaluate_sources_quality: {err}") # Логирование ошибок
            sources_assessment[fact] = {
                "reliability_score": 30,
                "sources_count": source_count,
                "authoritative_sources": False,
                "consensus": "Не удалось определить",
                "summary": "Возникла ошибка при оценке качества источников",
                "top_source": sources[0] if sources else None,
                "source_diversity": "Не определено"
            } # Возврат стандартного ответа при ошибке
    
    return sources_assessment

async def generate_comprehensive_assessment(text_analysis, facts, fact_results, sources_quality, factcheck_results):
    """Создает комплексную оценку с учетом всех компонентов анализа"""
    
    # Подсчитываем общую статистику источников
    total_sources = sum(len(sources) for sources in fact_results.values())
    facts_with_sources = sum(1 for sources in fact_results.values() if sources)
    
    data = {
        "text_analysis": text_analysis,
        "facts": facts,
        "fact_results": fact_results,
        "sources_quality": sources_quality, 
        "factcheck_results": factcheck_results,
        "sources_statistics": {
            "total_sources_found": total_sources,
            "facts_with_sources": facts_with_sources,
            "total_facts_checked": len(facts)
        }
    }
    
    data_str = json.dumps(data, ensure_ascii=False)
    prompt = f"""
Создай комплексную оценку достоверности новости на основе всех доступных данных.
НЕ используй markdown-форматирование, символы *, **, ##, [], (), ~, `, >, #, +, -, =, |.

КОМПОНЕНТЫ АНАЛИЗА:
1. Анализ текста новости (Источник (и его репутация), логика, непротиворечивость, возможность верификации, доказательства, технические аспекты(даты, участники, локации), качество журналистики)
2. Извлечение и проверка фактов по внешним источникам  
3. Сопоставление фактов с источниками
4. Оценка качества и количества источников

ОСОБОЕ ВНИМАНИЕ:
- Количество найденных источников для каждого факта
- Подтверждаются ли ключевые факты надежными источниками
- Есть ли искажения, манипуляции или преувеличения
- Представлены ли факты в корректном контексте
- Релевантность проверенных фактов основной теме

Верни отчет в формате:

📊 ОБЩАЯ ОЦЕНКА ДОСТОВЕРНОСТИ: [число от 0 до 100]

🔍 АНАЛИЗ ТЕКСТА: [оценка качества текста новости]

📋 ПРОВЕРКА ФАКТОВ: [результаты сопоставления с источниками]

📚 ИСТОЧНИКИ: [статистика и оценка источников - всего найдено, качество]

✅ ПОЛОЖИТЕЛЬНЫЕ АСПЕКТЫ: [список сильных сторон]

⚠️ ПРОБЛЕМНЫЕ МОМЕНТЫ: [список выявленных проблем]

💭 ИТОГОВОЕ ЗАКЛЮЧЕНИЕ: [финальный вывод о надежности]

Данные: {data_str}
"""
    try:
        resp = ollama.generate(
            model='yandex/YandexGPT-5-Lite-8B-instruct-GGUF',
            prompt=prompt,
            options={'temperature': 0.1, 'num_ctx': 16384}
        )
        return remove_thinking_tags(resp['response']) # Удаление маркеров мышления
    except Exception as err:
        logger.error(f"Ошибка в comprehensive_assessment: {err}") # Логирование ошибок
        return "Не удалось сформировать комплексную оценку." # Возврат сообщения об ошибке

async def anti_flood(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик защиты от флуда"""
    user_id = update.effective_user.id
    
    if not flood_control.check_user(user_id): # Проверка на флуд через контроллер
        remaining = flood_control.get_remaining_requests(user_id)
        logger.warning(f"Флуд-блокировка пользователя {user_id}") # Логирование блокировки
        await update.message.reply_text(
            f"⚠️ Превышен лимит запросов (15 в час).\n"
            f"Оставшиеся запросы: {remaining}\n" 
            f"Попробуйте через час."
        )
        return True  # Блокируем обработку
    return False  # Продолжаем обработку

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка входящих текстовых и пересланных сообщений"""
    try:
        # Проверка антифлуд
        if await anti_flood(update, context):
            return

        user_text = update.message.text or update.message.caption

        # Извлекаем текст из сообщения или подписи к медиа
        user_text = (
            update.message.text or 
            update.message.caption or 
            ''
        ).strip()

        # Проверяем наличие текста
        if not user_text:
            logger.debug("Медиа-сообщение без текста от %s", update.effective_user.id)
            return

        # Проверка длины текста
        text_length = len(user_text)
        if text_length < 10:
            await update.message.reply_text("📝 Текст слишком короткий для анализа. Минимум 10 символов.")
            return

        if len(user_text) > 2000:
            user_text = user_text[:2000] + "..." # лимит длинны сообщения

        logger.info(f"Received from {update.effective_user.id}: {user_text[:120]!r}") # Логирование получения сообщения

        # Показываем оставшиеся запросы
        remaining = flood_control.get_remaining_requests(update.effective_user.id)
        
        processing_message = await update.message.reply_text(
            f"⏳ Анализирую информацию...\n"
            f"📊 Оставшиеся запросы: {remaining}/15"
        )

        try:
            await context.bot.edit_message_text(
                chat_id=update.effective_message.chat_id,
                message_id=processing_message.message_id,
                text="⏳ Выполняю анализ текста и извлечение фактов..."
            )
        except Exception as e:
            logger.warning(f"Не удалось обновить сообщение: {e}") # Логирование ошибок при обновлении сообщения
        
        text_analysis_task = asyncio.create_task(analyze_news_text(user_text)) # Создание задачи анализа текста
        facts_data = await analyze_facts(user_text)
        facts = facts_data.get('facts', [])[:6] # лимит фактов
        
        # Получаем результаты проверки фактов
        try:
            await context.bot.edit_message_text(
                chat_id=update.effective_message.chat_id,
                message_id=processing_message.message_id,
                text=f"⏳ Проверяю {len(facts)} извлеченных фактов..."
            )
        except Exception as e:
            logger.warning(f"Не удалось обновить сообщение: {e}")
        
        fact_results = {fact: await yandex_factcheck(fact) for fact in facts} # Получение результатов проверки
        
        # Получаем результат анализа текста
        try:
            await context.bot.edit_message_text(
                chat_id=update.effective_message.chat_id,
                message_id=processing_message.message_id,
                text="⏳ Анализирую качество текста..."
            )
        except Exception as e:
            logger.warning(f"Не удалось обновить сообщение: {e}")
            
        text_analysis = await text_analysis_task
        
        # Оцениваем качество источников
        try:
            await context.bot.edit_message_text(
                chat_id=update.effective_message.chat_id,
                message_id=processing_message.message_id,
                text="⏳ Оцениваю качество и количество источников..."
            )
        except Exception as e:
            logger.warning(f"Не удалось обновить сообщение: {e}")

        sources_quality_task = asyncio.create_task(evaluate_sources_quality(fact_results)) # Создание задачи оценки источников
        
        # Выполняем проверку фактов
        try:
            await context.bot.edit_message_text(
                chat_id=update.effective_message.chat_id,
                message_id=processing_message.message_id,
                text="⏳ Выполняю проверку фактов..."
            )
        except Exception as e:
            logger.warning(f"Не удалось обновить сообщение: {e}")

        factcheck_task = asyncio.create_task(perform_factchecking(user_text, facts, fact_results)) # Создание задачи проверки фактов
        
        # Ждем завершения всех задач
        sources_quality = await sources_quality_task
        factcheck_results = await factcheck_task
        
        # Формируем комплексную оценку
        try:
            await context.bot.edit_message_text(
                chat_id=update.effective_message.chat_id,
                message_id=processing_message.message_id,
                text="⏳ Формирую комплексную оценку с учетом источников..."
            )
        except Exception as e:
            logger.warning(f"Не удалось обновить сообщение: {e}")

        comprehensive_report = await generate_comprehensive_assessment(
            text_analysis, facts, fact_results, sources_quality, factcheck_results
        ) # Генерация итогового отчёта
        
        # Объединенный блок результатов проверки и источников
        combined_results = "\n📑 РЕЗУЛЬТАТЫ ПРОВЕРКИ:\n"
        total_sources = 0
        
        if "factcheck_results" in factcheck_results:
            for i, fact_check in enumerate(factcheck_results["factcheck_results"][:3], 1): # Ограничиваем для экономии места
                fact = fact_check.get("fact", "")
                status = fact_check.get("source_confirmation", "")
                accuracy = fact_check.get("accuracy_level", "")
                sources_count = fact_check.get("source_count", 0)
                confidence = fact_check.get("confidence_score", 0)
                
                total_sources += sources_count
                
                combined_results += f"{i}. {fact[:150]}{'...' if len(fact) > 150 else ''}\n"
                combined_results += f"   Подтверждение: {status}\n"
                combined_results += f"   Точность: {accuracy}, Уверенность: {confidence}%\n"
                combined_results += f"   Источников найдено: {sources_count}\n"
                
                # Добавляем топ-источник если есть
                if fact in fact_results and fact_results[fact]:
                    top_source = fact_results[fact][0]
                    title = top_source.get('title', 'Без заголовка')
                    url = top_source.get('url', '')
                    combined_results += f"   Топ-источник: {title[:100]}{'...' if len(title) > 100 else ''}\n"
                    if url:
                        combined_results += f"   Ссылка: {url[:200]}{'...' if len(url) > 200 else ''}\n"
                combined_results += "\n"
        
        combined_results += f"📊\n"
        
        # Формируем полный отчет
        final_report = "\n".join([
            comprehensive_report[:3500],
            combined_results[:3500]
        ]) # Объединение частей отчёта
        
        # Удаляем сообщение о обработке
        try:
            await context.bot.delete_message(
                chat_id=update.effective_message.chat_id,
                message_id=processing_message.message_id
            )
        except Exception as e:
            logger.warning(f"Не удалось удалить сообщение о обработке: {e}") # Логирование ошибок при удалении сообщения
        
        # Отправляем отчет
        await send_long_message(update, final_report) # Отправка сообщения
        
    except Exception as err:
        logger.error(f"Ошибка handle_message: {err}", exc_info=True) # Логирование ошибок
        await update.message.reply_text("⚠️ Ошибка при обработке запроса.")

async def send_long_message(update, text):
    """Отправляет сообщение, сокращая его при необходимости до одного сообщения"""
    # Максимальная длина сообщения в Telegram
    MAX_MESSAGE_LENGTH = 4000  # Немного меньше официального лимита для подстраховки
    
    # Если сообщение короче максимальной длины, отправляем его целиком
    if len(text) <= MAX_MESSAGE_LENGTH:
        return await update.message.reply_text(text)
    
    # Сокращаем сообщение до допустимого размера
    beginning_length = MAX_MESSAGE_LENGTH // 2
    ending_length = MAX_MESSAGE_LENGTH - beginning_length - 150  # 150 символов на сообщение о сокращении
    
    beginning = text[:beginning_length]
    ending = text[-ending_length:]
    
    shortened_message = (
        f"{beginning}\n\n"
        f"[...сообщение сокращено из-за ограничений Telegram...]\n\n"
        f"{ending}"
    )
    
    return await update.message.reply_text(shortened_message)

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Глобальный обработчик исключений"""
    logger.error("Глобальная ошибка", exc_info=context.error) # Логирование глобальной ошибки
    
    # Если возможно, сообщаем пользователю о проблеме
    if update and update.effective_message:
        await update.effective_message.reply_text(
            "🚨 Произошла системная ошибка. Пожалуйста, попробуйте позже."
        ) # Сообщение пользователю

if __name__ == '__main__':
    app = Application.builder().token(TELEGRAM_TOKEN).build() # Создание приложения
    # Обработчик всех сообщений кроме команд
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler) # Глобальный обработчик ошибок

    logger.info("Бот фактчекинга запущен с новыми функциями")
    app.run_polling() # Запуск бота в режиме опроса
