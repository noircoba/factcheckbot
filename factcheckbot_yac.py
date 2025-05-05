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
    """Извлечение проверяемых фактов из текста с помощью LLM"""
    prompt = f"""
Проанализируй новостной текст и выдели из него проверяемые факты для дальнейшей верификации.

ИНСТРУКЦИИ:
1. Каждый факт должен содержать полный контекст: ЧТО произошло, ГДЕ, КОГДА, КТО участвовал/сообщил, и любые дополнительные детали.
2. Включай в факты временные маркеры (дату, время события).
3. Сохраняй числовые данные, имена, названия организаций, географические названия.
4. Формулируй факты как законченные предложения, понятные вне контекста исходной статьи.
5. Не включай оценочные суждения или мнения, только проверяемые утверждения.

ПРИМЕР неправильного извлечения (слишком мало контекста):
"Несколько землетрясений произошли сегодня"
"Самое сильное землетрясение произошло в 11:27 мск"

ПРИМЕР правильного извлечения (с полным контекстом):
"Несколько землетрясений произошли [дата] в районе водопада Учан-Су в Крыму, согласно сообщению РИА Новости Крым"
"Самое сильное землетрясение в районе водняка Учан-Су в Крыму [дата] произошло в 11:27 по московскому времени, по данным замдиректора Института сейсмологии и геодинамики Марины Бондарь"

Верни ТОЛЬКО JSON без пояснений:
{{
    "facts": ["полный факт 1", "полный факт 2", ...]
}}

Текст: {text[:1500]}
"""
    logger.info(f"LLM Fact Extraction Prompt: {text[:1500]!r}") # Логирование входного запроса
    try:
        resp = ollama.generate(
            model='yandex/YandexGPT-5-Lite-8B-instruct-GGUF',
            prompt=prompt,
            format='json',
            options={'temperature': 0.1, 'num_ctx': 8192}
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
        logger.info(f"Исходный факт: '{original_fact}'") # Логирование исходного факта
        
        # Формируем XML запрос согласно документации
        request_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
        <request>
            <query>{original_fact}</query>
            <page>0</page>
            <sortby order="descending">rlv</sortby>
            <maxpassages>2</maxpassages>
            <groupings>
                <groupby attr="d" mode="deep" groups-on-page="5" docs-in-group="1"/>
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
        
        logger.info(f"Отправляем запрос к Yandex Search API: {original_fact[:50]}...") # Логирование отправки запроса
        await asyncio.sleep(uniform(0.5, 1.5)) # Рандомная задержка для избежания флуда
        
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
                snippet = ' '.join([p.text for p in doc.find_all('passage')][:2]) # Обрезание отрывков
        
                results.append({
                    'title': title[:200],
                    'url': url,
                    'snippet': snippet[:400]
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

async def analyze_news_text(text: str) -> str:
    """Анализ текста новости на предмет внутренней согласованности, признаков манипуляции и т.д."""
    # Ограничиваем длину анализируемого текста
    truncated_text = text[:1000] + ("..." if len(text) > 1000 else "") # Обрезка длинного текста
    
    prompt = f"""
Проанализируй текст новости и оцени его достоверность по внутренним признакам:
1. Эмоциональная окраска и язык (нейтральный или манипулятивный)
2. Логические противоречия внутри текста
3. Наличие/отсутствие конкретики (даты, имена, цифры)
4. Ссылки на источники, (есть или нет, авторитетные или нет) отсутствие авторитетных источников не должно сильно снижать достоверность, они могут быть оббнаружены в резльтате поиска в Интернет
5. Баланс точек зрения (присутствуют разные стороны или однобокое освещение)

Дай краткую оценку надежности текста как источника информации (не более 200 слов).

Текст новости: {truncated_text}
"""
    try:
        resp = ollama.generate(
            model='yandex/YandexGPT-5-Lite-8B-instruct-GGUF',
            prompt=prompt,
            options={'temperature': 0.1, 'num_ctx': 8192}
        )
        result = remove_thinking_tags(resp['response']) # Удаление маркеров мышления
        return result
    except Exception as err:
        logger.error(f"Ошибка analyze_news_text: {err}") # Логирование ошибок
        return "Не удалось выполнить анализ текста новости." # Возврат сообщения об ошибке

def remove_thinking_tags(text):
    """Удаляет содержимое между тегами """
    import re
    pattern = r'{<think>.*?</think>' # Регулярное выражение для поиска маркеров мышления
    # Используем re.DOTALL, чтобы точка соответствовала также символам новой строки
    cleaned_text = re.sub(pattern, '', text, flags=re.DOTALL)
    return cleaned_text

async def generate_report(facts: list, results: dict) -> str:
    """Формирование окончательного отчёта с помощью LLM"""
    data_str = json.dumps(results, ensure_ascii=False, indent=2)
    logger.info(f"LLM Report Data: {data_str}") # Логирование данных
    prompt = f"""
Ты должен создать отчет о достоверности информации на основе проверенных фактов.

ФОРМАТ ВЫВОДА:
Создай простой текстовый отчет БЕЗ использования Markdown-форматирования.
НЕ используй символы *, _, [, ], (, ), ~, `, >, #, +, -, =, |, {{, }}, ., ! для форматирования.
Уберери иероглифы из ответа.
Ограничь общий объем ответа в 3000 символов.

Структура отчета:
- Достоверность: X из 100

- Причины (список из нескольких пунктов)

- Источники (количество источников)

- Выводы на основе анализа данных и проверки в источниках

НЕ включай теги 🧠 или любые другие HTML-теги в свой ответ.
НЕ ссылайся на MarkdownV2 или форматирование в тексте ответа.

Данные: {data_str}
"""
    try:
        resp = ollama.generate(
            model='yandex/YandexGPT-5-Lite-8B-instruct-GGUF',
            prompt=prompt,
            options={'temperature': 0.1, 'num_ctx': 8192}
        )
        response_text = resp['response']
        
        # Удаляем содержимое между тегами <think> и< /think>
        clean_response = remove_thinking_tags(response_text)
        
        return clean_response
    except Exception as err:
        logger.error(f"Ошибка generate_report: {err}") # Логирование ошибок
        return "Не удалось сгенерировать отчёт." # Возврат сообщения об ошибке

def is_meaningful_text(text: str) -> bool:
    """Проверяет осмысленность текста"""
    clean_text = re.sub(r'\s+', ' ', re.sub(r'[^\w\s]', '', text)).strip() # Очистка текста от специальных символов
    return (
        len(clean_text) >= 16 and # Минимальная длина
        len(set(clean_text.split())) >= 4 and # Разнообразие слов
        any(c.isalpha() for c in clean_text) # Наличие букв
    )

async def anti_flood(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик защиты от флуда"""
    user_id = update.effective_user.id
    
    if not flood_control.check_user(user_id): # Проверка на флуд через контроллер
        logger.warning(f"Флуд-блокировка пользователя {user_id}") # Логирование блокировки
        await update.message.reply_text("⚠️ Превышен лимит запросов. Попробуйте через 5 минут.")
        return True  # Блокируем обработку
    return False  # Продолжаем обработку

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка входящих текстовых и пересланных сообщений"""
    try:
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
        if text_length < 6:
            logger.debug("Короткий текст в медиа-сообщении от %s", update.effective_user.id)
            return

        if len(user_text) > 1500:
            user_text = user_text[:1500] + "..." # Обрезка длинного текста

        logger.info(f"Received from {update.effective_user.id}: {user_text[:120]!r}") # Логирование получения сообщения

        processing_message = await update.message.reply_text("⏳ Анализирую информацию...")

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
        facts = facts_data.get('facts', [])[:5] # Ограничение количества фактов
        
        # Получаем результаты проверки фактов
        try:
            await context.bot.edit_message_text(
                chat_id=update.effective_message.chat_id,
                message_id=processing_message.message_id,
                text="⏳ Проверяю извлеченные факты..."
            )
        except Exception as e:
            logger.warning(f"Не удалось обновить сообщение: {e}")
        
        fact_results = {fact: await yandex_factcheck(fact) for fact in facts} # Получение результатов проверки
        
        # Получаем результат анализа текста
        try:
            await context.bot.edit_message_text(
                chat_id=update.effective_message.chat_id,
                message_id=processing_message.message_id,
                text="⏳ Анализирую текст..."
            )
        except Exception as e:
            logger.warning(f"Не удалось обновить сообщение: {e}")
            
        # Получаем результат задачи анализа текста - ДОБАВЛЕНО
        text_analysis = await text_analysis_task
        
        # Оцениваем качество источников
        try:
            await context.bot.edit_message_text(
                chat_id=update.effective_message.chat_id,
                message_id=processing_message.message_id,
                text="⏳ Оцениваю качество источников..."
            )
        except Exception as e:
            logger.warning(f"Не удалось обновить сообщение: {e}")

        sources_quality_task = asyncio.create_task(evaluate_sources_quality(fact_results)) # Создание задачи оценки источников
        
        # Выполняем проверку фактов на соответствие источникам
        try:
            await context.bot.edit_message_text(
                chat_id=update.effective_message.chat_id,
                message_id=processing_message.message_id,
                text="⏳ Выполняю проверку фактов в источниках..."
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
                text="⏳ Формирую комплексную оценку..."
            )
        except Exception as e:
            logger.warning(f"Не удалось обновить сообщение: {e}")

        comprehensive_report = await generate_comprehensive_assessment(
            text_analysis, facts, fact_results, sources_quality, factcheck_results
        ) # Генерация итогового отчёта
        
        # Добавляем блок с результатами проверки фактов
        factcheck_info = "\n 📑 РЕЗУЛЬТАТЫ ПРОВЕРКИ ФАКТОВ:\n"
        if "factcheck_results" in factcheck_results:
            for fact_check in factcheck_results["factcheck_results"]:
                fact = fact_check.get("fact", "")
                status = fact_check.get("confirmation_status", "")
                explanation = fact_check.get("explanation", "")
                factcheck_info += f"• {fact[:120]}{'...' if len(fact) > 120 else ''}\n"
                factcheck_info += f"  Статус: {status}\n"
                factcheck_info += f"  Пояснение: {explanation}\n\n"
        
        # Добавляем блок с детальными ссылками на источники
        sources_info = "🔗 ИСТОЧНИКИ ФАКТОВ:\n"
        for fact, sources in fact_results.items():
            sources_info += f"• {fact[:120]}{'...' if len(fact) > 120 else ''}\n"
            
            if not sources:
                sources_info += "  - Источники не найдены\n"
            else:
                for src in sources[:1]:
                    title = src.get('title', 'Без заголовка')
                    url = src.get('url', '')
                    sources_info += f"  - {title[:120]}{'...' if len(title) > 120 else ''}: {url[:120]}\n\n"
        
        # Формируем полный отчет
        final_report = "\n".join([
            comprehensive_report[:1330],
            factcheck_info[:1330],
            sources_info[:1330]
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
    # Сохраняем начало (основную оценку) и конец (источники)
    beginning_length = MAX_MESSAGE_LENGTH // 2
    ending_length = MAX_MESSAGE_LENGTH - beginning_length - 140  # 140 символов на сообщение о сокращении
    
    beginning = text[:beginning_length]
    ending = text[-ending_length:]
    
    shortened_message = (
        f"{beginning}\n\n"
        f"[...сообщение сокращено из-за ограничений Telegram...]\n\n"
        f"{ending}"
    )
    
    return await update.message.reply_text(shortened_message)

async def analyze_news_text(text: str) -> dict:
    """Анализ текста новости с возвратом структурированной оценки"""
    truncated_text = text[:1500] + ("..." if len(text) > 1500 else "") # Обрезка длинного текста
    
    prompt = f"""
Проанализируй текст новости по внутренним признакам и верни результат в JSON формате:
{{
  "textual_credibility_score": 0-100,
  "language_assessment": "нейтральный/манипулятивный и почему",
  "internal_consistency": "оценка внутренней логики и непротиворечивости",
  "specificity": "оценка конкретики (даты, имена, цифры)",
  "sources_cited": "анализ упоминаемых в тексте источников",
  "balance": "оценка баланса точек зрения",
  "conclusion": "краткий вывод об общей надежности текста"
}}

Текст новости: {truncated_text}
"""
    try:
        resp = ollama.generate(
            model='yandex/YandexGPT-5-Lite-8B-instruct-GGUF',
            prompt=prompt,
            format='json',
            options={'temperature': 0.1, 'num_ctx': 8192}
        )
        
        # Обработка результата и извлечение JSON
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
            "textual_credibility_score": 50,
            "language_assessment": "Не удалось оценить",
            "internal_consistency": "Не удалось оценить",
            "specificity": "Не удалось оценить",
            "sources_cited": "Не удалось оценить",
            "balance": "Не удалось оценить",
            "conclusion": "Не удалось выполнить анализ текста новости."
        } # Возврат стандартного ответа при ошибке

async def generate_comprehensive_assessment(text_analysis, facts, fact_results, sources_quality, factcheck_results):
    """Создает комплексную оценку с учетом проверки фактов и качества источников"""
    data = {
        "text_analysis": text_analysis,
        "facts": facts,
        "fact_results": fact_results,
        "sources_quality": sources_quality,
        "factcheck_results": factcheck_results
    }
    
    data_str = json.dumps(data, ensure_ascii=False)
    prompt = f"""
Создай комплексную оценку достоверности новости на основе всех доступных данных:
Не используй Markdown
Не используй ** ## для выделения разделов и абзацев лучше использовать одинарные • или - для пунктов и ЗАГЛАВНЫЕ буквы для разделов

1. Анализ текста (стиль, внутренняя непротиворечивость)
2. Проверка фактов по внешним источникам
3. Согласованность фактов с источниками (fact-checking)
4. Качество и надежность источников

Особенно учитывай:
- Подтверждаются ли ключевые факты надежными источниками
- Есть ли искажения, манипуляции или преувеличения
- Представлены ли факты в корректном контексте
- Сбалансированность представления информации

Верни отчет в таком формате:
- 📶 ОБЩАЯ ОЦЕНКА ДОСТОВЕРНОСТИ: число от 0 до 100 из 100
[обязятельно вставить пустоую строку с переносом строки]

- 🔰 СООТВЕТСТВИЕ ФАКТАМ: оценка согласованности с фактами из источников
[обязятельно вставить пустоую строку с переносом строки]

- 💭 ВЫВОД: общее заключение о надежности информации
[обязятельно вставить пустоую строку с переносом строки]

- ✅ СИЛЬНЫЕ СТОРОНЫ: список положительных аспектов новости
[обязятельно вставить пустоую строку с переносом строки]

- ⚠️ ПРОБЛЕМНЫЕ МЕСТА: список проблем с достоверностью
[обязятельно вставить пустоую строку с переносом строки]

Данные: {data_str}
"""
    try:
        resp = ollama.generate(
            model='yandex/YandexGPT-5-Lite-8B-instruct-GGUF',
            prompt=prompt,
            options={'temperature': 0.1, 'num_ctx': 8192}
        )
        return remove_thinking_tags(resp['response']) # Удаление маркеров мышления
    except Exception as err:
        logger.error(f"Ошибка в comprehensive_assessment: {err}") # Логирование ошибок
        return "Не удалось сформировать комплексную оценку." # Возврат сообщения об ошибке

async def evaluate_sources_quality(fact_results: dict) -> dict:
    """Оценивает качество и надежность найденных источников"""
    sources_assessment = {}
    
    for fact, sources in fact_results.items():
        if not sources:
            sources_assessment[fact] = {
                "reliability_score": 0,
                "sources_count": 0,
                "authoritative_sources": False,
                "consensus": "Нет данных",
                "summary": "Источники не найдены"
            }
            continue
        
        # Собираем данные для оценки качества источников
        sources_data = []
        for src in sources:
            url = src.get('url', '')
            title = src.get('title', '')
            snippet = src.get('snippet', '')
            
            # Подготавливаем данные для LLM
            sources_data.append({
                'url': url,
                'title': title,
                'snippet': snippet[:150]
            })
        
        # Оцениваем качество источников с помощью LLM
        prompt = f"""
Оцени качество и надежность источников для проверки факта: "{fact}"

Данные:
{json.dumps(sources_data, ensure_ascii=False)}

Верни оценку в формате JSON:
{{
  "reliability_score": число от 0 до 100 из 100,
  "sources_count": количество представленных источников,
  "authoritative_sources": true/false - есть ли среди источников авторитетные СМИ/организации,
  "consensus": "согласуются ли источники между собой",
  "summary": "краткий вывод о качестве источников"
}}
"""
        try:
            resp = ollama.generate(
                model='yandex/YandexGPT-5-Lite-8B-instruct-GGUF',
                prompt=prompt,
                format='json',
                options={'temperature': 0.1, 'num_ctx': 8192}
            )
            
            raw = resp['response'].strip().replace('```json', '').replace('```', '')
            try:
                assessment = json.loads(raw)
            except json.JSONDecodeError:
                import json_repair
                assessment = json.loads(json_repair.repair_json(raw))
                
            sources_assessment[fact] = assessment # Сохранение оценки
        except Exception as err:
            logger.error(f"Ошибка evaluate_sources_quality: {err}") # Логирование ошибок
            sources_assessment[fact] = {
                "reliability_score": 30,
                "sources_count": len(sources),
                "authoritative_sources": False,
                "consensus": "Не удалось определить",
                "summary": "Возникла ошибка при оценке качества источников"
            } # Возврат стандартного ответа при ошибке
    
    return sources_assessment

async def perform_factchecking(user_text, facts, fact_results):
    """Выполняем проверку фактов из новости и их согласованность с источниками"""
    
    # Подготовка данных для анализа
    factcheck_data = {
        "original_text": user_text[:1000],
        "facts_to_check": facts,
        "sources_data": fact_results
    }
    
    data_str = json.dumps(factcheck_data, ensure_ascii=False)
    
    prompt = f"""
Выполни детальную проверку фактов (fact-checking) между текстом новости и найденными источниками.
Для каждого факта оцени:


[ 🌐 ]
1. Подтверждается ли факт источниками (полностью/частично/не подтверждается/противоречит)
2. Точность изложения факта в новости (искажен/преувеличен/корректен)
3. Контекст представления факта (полный/неполный/вне контекста)

Верни результат в формате JSON:
{{
  "factcheck_results": [
    {{
      "fact": "проверяемый факт",
      "confirmation_status": "подтвержден/частично подтвержден/не подтвержден/противоречит источникам",
      "accuracy": "точно/с искажениями/с преувеличениями",
      "context_completeness": "полный/неполный/вне контекста",
      "explanation": "краткое объяснение результата проверки"
    }},
    // другие факты...
  ],
  "overall_factcheck_score": число от 0 до 100,
  "overall_assessment": "общая оценка соответствия фактов источникам"
}}

Данные: {data_str}
"""
    
    try:
        resp = ollama.generate(
            model='yandex/YandexGPT-5-Lite-8B-instruct-GGUF',
            prompt=prompt,
            format='json',
            options={'temperature': 0.1, 'num_ctx': 8192}
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
                    "confirmation_status": "не определено",
                    "accuracy": "не определено",
                    "context_completeness": "не определено",
                    "explanation": "Произошла ошибка при проверке фактов"
                }
            ],
            "overall_factcheck_score": 50,
            "overall_assessment": "Не удалось выполнить полноценную проверку фактов" # Возврат стандартного ответа
        }

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Глобальный обработчик исключений"""
    logger.error("Глобальная ошибка", exc_info=context.error) # Логирование глобальной ошибки
    
    # Если возможно, сообщаем пользователю о проблеме
    if update and update.effective_message:
        await update.effective_message.reply_text(
            "🚨 Произошла системная ошибка. Пожалуйста, попробуйте позже."
        ) # Сообщение пользователю
    
    # Можно добавить оповещение администратора о критической ошибке
    # например, отправкой сообщения в специальный чат или канал

if __name__ == '__main__':
    app = Application.builder().token(TELEGRAM_TOKEN).build() # Создание приложения
    # Обработчик всех сообщений кроме команд
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler) # Глобальный обработчик ошибок

    logger.info("Бот запущен")
    app.run_polling() # Запуск бота в режиме опроса
