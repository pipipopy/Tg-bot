import asyncio
import random
import time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
    CallbackQueryHandler,
    ConversationHandler
)
from tinkoff.invest import AsyncClient, InstrumentStatus, InstrumentIdType
from tinkoff.invest.utils import quotation_to_decimal
from datetime import datetime, timezone
import logging

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Конфигурация
TINKOFF_TOKEN = "t.qZQAlxmuWxV7j6MglXaFl4Pfyd0s9MENH2ZEYq1azGDk_uSYr9RYHtqs-rhz6v8F-1ZPqwRP_2oLulHAXgX80g"
TELEGRAM_TOKEN = "8001683378:AAFLoT-ENDhf5oT8paDU17lH0srlfBZN0Ec"

# Глобальный кеш облигаций
bond_cache = []
bond_details_cache = {}  # Кеш для деталей облигаций
last_cache_update = None

# Маппинг ключевых слов для секторов
SECTOR_KEYWORDS = {
    "Финансы": ["финанс", "банк", "страх", "инвест", "financial"],
    "Нефть и газ": ["нефт", "газ", "нефте", "нефтегаз", "oil", "gas"],
    "Телекмуникации": ["телеком", "связь", "теле", "коммуникац", "telecom"],
    "Транспорт": ["транспорт", "авиа", "железн", "авто", "transport"],
    "Металлургия": ["металл", "сталь", "металлург", "метал", "metallurgy"],
    "Электроэнергетика": ["энерг", "электро", "электрич", "энергетик", "electric"],
    "Потребительские товары": ["потреб", "товар", "розниц", "consumer", "retail"],
    "Недвижимость": ["недвиж", "строит", "девелоп", "real estate", "development"],
    "Государственные": ["государств", "гос", "government", "суверен", "федеральн"],
    "Муниципальные": ["муниципальн", "городск", "областн", "municipal", "субфедеральн"],
    "Другие": []  # Все остальные облигации
}

# Доступные секторы экономики (убрали "Химию")
SECTORS = [
    "Финансы", "Нефть и газ", "Телекмуникации", "Транспорт", 
    "Металлургия", "Электроэнергетика", "Потребительские товары",
    "Недвижимость", "Государственные", "Муниципальные", "Другие"
]

# Состояния для диалога поиска
SECTOR_SELECTION, MIN_RATE, LIMIT = range(3)

async def get_bond_details(figi: str):
    # Проверяем кеш
    if figi in bond_details_cache:
        cached_data, timestamp = bond_details_cache[figi]
        # Если данные в кеше не старше 5 минут - используем их
        if time.time() - timestamp < 300:
            return cached_data
    
    async with AsyncClient(TINKOFF_TOKEN) as client:
        try:
            response = await client.instruments.bond_by(id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_FIGI, id=figi)
            bond = response.instrument
            
            nominal = float(quotation_to_decimal(bond.nominal))
            coupon_quantity_per_year = bond.coupon_quantity_per_year
            
            coupon_rate_percent = None
            coupon_payment = None
            
            if hasattr(bond, "coupon_percent") and bond.coupon_percent is not None:
                coupon_rate_percent = bond.coupon_percent
            else:
                # Добавляем задержку перед запросом купонов
                await asyncio.sleep(0.1)
                coupons_response = await client.instruments.get_bond_coupons(figi=figi)
                if coupons_response.events:
                    now = datetime.now(timezone.utc)
                    for coupon in coupons_response.events:
                        coupon_date = coupon.coupon_date.replace(tzinfo=timezone.utc)
                        if coupon_date > now:
                            if hasattr(coupon, "pay_one_bond") and coupon.pay_one_bond:
                                coupon_payment = float(quotation_to_decimal(coupon.pay_one_bond))
                            break
            
            if coupon_rate_percent and not coupon_payment and coupon_quantity_per_year > 0:
                coupon_payment = (nominal * coupon_rate_percent / 100) / coupon_quantity_per_year
                
            if coupon_payment and not coupon_rate_percent and nominal > 0 and coupon_quantity_per_year > 0:
                coupon_rate_percent = (coupon_payment * coupon_quantity_per_year / nominal) * 100
            
            # Рассчитываем дней до погашения
            days_to_maturity = 0
            if bond.maturity_date:
                maturity_date = bond.maturity_date.replace(tzinfo=timezone.utc)
                days_to_maturity = (maturity_date - datetime.now(timezone.utc)).days
            
            # Определяем сектор по ключевым словам
            sector_name = "Другие"  # По умолчанию
            if hasattr(bond, 'sector') and bond.sector:
                bond_sector = bond.sector.lower()
                
                # Ищем подходящий сектор по ключевым словам
                for sector, keywords in SECTOR_KEYWORDS.items():
                    if sector == "Другие":
                        continue  # Пропускаем "Другие" при проверке
                        
                    if any(keyword in bond_sector for keyword in keywords):
                        sector_name = sector
                        break
            
            result = {
                "name": bond.name,
                "ticker": bond.ticker,
                "figi": bond.figi,
                "currency": bond.nominal.currency,
                "nominal": nominal,
                "coupon_rate_percent": coupon_rate_percent,
                "coupon_payment": coupon_payment,
                "coupon_quantity_per_year": coupon_quantity_per_year,
                "maturity_date": bond.maturity_date.strftime("%d.%m.%Y") if bond.maturity_date else "Неизвестно",
                "days_to_maturity": days_to_maturity,
                "sector": sector_name
            }

            # Сохраняем в кеш
            bond_details_cache[figi] = (result, time.time())
            return result
        except Exception as e:
            logger.error(f"Ошибка при получении данных об облигации {figi}: {str(e)}", exc_info=True)
            return None

async def get_all_bonds():
    global bond_cache, last_cache_update
    
    # Используем кеш, если он актуален (обновляем раз в 10 минут)
    if bond_cache and last_cache_update and (time.time() - last_cache_update) < 600:
        return bond_cache
    
    async with AsyncClient(TINKOFF_TOKEN) as client:
        try:
            response = await client.instruments.bonds(
                instrument_status=InstrumentStatus.INSTRUMENT_STATUS_BASE
            )
            if not response.instruments:
                return []
            
            filtered_bonds = []
            for bond in response.instruments:
                if (bond.currency == 'rub' and 
                    not bond.for_qual_investor_flag and 
                    not bond.floating_coupon_flag and 
                    bond.buy_available_flag and 
                    bond.sell_available_flag):
                    filtered_bonds.append(bond)
            
            bond_cache = filtered_bonds
            last_cache_update = time.time()
            logger.info(f"Обновлен кеш облигаций. Найдено {len(bond_cache)} облигаций")
            return bond_cache
        except Exception as e:
            logger.error(f"Ошибка при загрузке облигаций: {str(e)}", exc_info=True)
            return bond_cache if bond_cache else []

async def find_and_send_bonds(message, min_rate, sector=None, limit=5):
    all_bonds = await get_all_bonds()
    if not all_bonds:
        await message.reply_text("❌ Не найдено подходящих облигаций.")
        return

    # Перемешиваем облигации для разнообразия
    shuffled_bonds = random.sample(all_bonds, len(all_bonds))
    found_count = 0
    processed_count = 0

    # Сообщаем о начале поиска
    await message.reply_text(f"🔍 Начинаю поиск по {len(shuffled_bonds)} облигациям...")

    for bond in shuffled_bonds:
        if found_count >= limit:
            break
            
        try:
            details = await get_bond_details(bond.figi)
            processed_count += 1
            
            if not details:
                continue
                
            # Проверяем ставку
            coupon_rate = details.get('coupon_rate_percent')
            if coupon_rate is None or coupon_rate < min_rate:
                continue
                
            # Проверяем сектор (если указан)
            if sector:
                if details.get('sector') != sector:
                    continue
            
            # Отправляем найденную облигацию сразу
            found_count += 1
            await send_bond_details(message, details)
            
            # Обновляем статус каждые 20 обработанных облигаций
            if processed_count % 20 == 0:
                await message.reply_text(f"⏳ Проверено {processed_count}/{len(shuffled_bonds)} облигаций...")
                
        except Exception as e:
            logger.error(f"Ошибка при обработке облигации {bond.figi}: {str(e)}")
            continue

    # Финал поиска
    if found_count == 0:
        reason = "не найдено облигаций, удовлетворяющих критериям"
        if sector:
            reason += f" в секторе '{sector}'"
        await message.reply_text(f"❌ {reason}. Попробуйте изменить параметры поиска.")
    else:
        await message.reply_text(f"✅ Поиск завершен! Проверено {processed_count} облигаций, найдено {found_count} подходящих.")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "🔍 <b>Бот для поиска облигаций с фиксированным купоном</b>\n\n"
        "Я помогу найти облигации, соответствующие вашим критериям.\n\n"
        "Используйте команды:\n"
        "/start - показать это сообщение\n"
        "/random - показать случайную облигацию\n"
        "/search - найти облигации по критериям"
    )
    await update.message.reply_text(help_text, parse_mode='HTML')

async def random_bond(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        all_bonds = await get_all_bonds()
        if not all_bonds:
            await update.message.reply_text("❌ Не найдено подходящих облигаций")
            return
            
        bond = random.choice(all_bonds)
        bond_details = await get_bond_details(bond.figi)
        
        if not bond_details:
            await update.message.reply_text("❌ Не удалось получить данные об облигации")
            return
            
        await send_bond_details(update.message, bond_details)
        
    except Exception as e:
        error_msg = f"⚠️ Ошибка: {str(e)}"
        logger.exception(error_msg)
        await update.message.reply_text(error_msg)

async def send_bond_details(message, bond_details: dict):
    # Формируем ссылку для Тинькофф Инвестиций
    tinkoff_link = f"https://www.tinkoff.ru/invest/bonds/{bond_details['ticker']}/"
    
    text = f"<b>{bond_details['name']}</b>\n\n"
    text += f"• <b>Тикер</b>: <a href='{tinkoff_link}'>{bond_details['ticker']}</a>\n"
    text += f"• <b>Сектор</b>: {bond_details.get('sector', 'Другие')}\n"
    text += f"• <b>Номинал</b>: {bond_details['nominal']:.2f} {bond_details['currency']}\n"
    
    if bond_details['coupon_rate_percent'] is not None:
        text += f"• <b>Купонная ставка</b>: {bond_details['coupon_rate_percent']:.2f}%\n"
    
    if bond_details['coupon_payment'] is not None:
        text += f"• <b>Купонный платеж</b>: {bond_details['coupon_payment']:.2f} {bond_details['currency']}\n"
    
    if bond_details['coupon_quantity_per_year'] > 0:
        text += f"• <b>Выплат в год</b>: {bond_details['coupon_quantity_per_year']}\n"
    
    if bond_details['days_to_maturity'] > 0:
        years = bond_details['days_to_maturity'] // 365
        months = (bond_details['days_to_maturity'] % 365) // 30
        text += f"• <b>До погашения</b>: ~{years} лет {months} мес.\n"
    
    text += f"• <b>Дата погашения</b>: {bond_details['maturity_date']}\n"
    
    await message.reply_text(text, parse_mode='HTML', disable_web_page_preview=True)

async def search_bonds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Отправляем клавиатуру с секторами
    keyboard = []
    for i in range(0, len(SECTORS), 3):
        row = [InlineKeyboardButton(SECTORS[j], callback_data=f"sector_{SECTORS[j]}") for j in range(i, min(i+3, len(SECTORS)))]
        keyboard.append(row)
    
    keyboard.append([InlineKeyboardButton("Любой сектор", callback_data="sector_any")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "🔍 Выберите сектор экономики:",
        reply_markup=reply_markup
    )
    
    # Возвращаем состояние выбора сектора
    return SECTOR_SELECTION

async def handle_sector_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    # Сохраняем выбранный сектор
    sector = query.data.replace("sector_", "")
    if sector == "any":
        context.user_data['sector'] = None
        sector_text = "Любой"
    else:
        context.user_data['sector'] = sector
        sector_text = sector
    
    await query.edit_message_text(
        text=f"✅ Сектор: {sector_text}\n\n"
             "📊 Введите минимальную купонную ставку в % (например: 8.5):"
    )
    return MIN_RATE

async def handle_min_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        min_rate = float(update.message.text)
        if min_rate <= 0 or min_rate > 30:
            await update.message.reply_text("❌ Пожалуйста, введите ставку от 0.1 до 30%")
            return MIN_RATE
            
        context.user_data['min_rate'] = min_rate
        
        # Запрашиваем количество облигаций
        await update.message.reply_text(
            "🔢 Сколько облигаций показать? (максимум 10):"
        )
        return LIMIT
    except ValueError:
        await update.message.reply_text("❌ Пожалуйста, введите число (например: 8.5)")
        return MIN_RATE

async def handle_limit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        limit = int(update.message.text)
        if limit < 1 or limit > 10:
            await update.message.reply_text("❌ Пожалуйста, введите число от 1 до 10")
            return LIMIT
            
        context.user_data['limit'] = limit
        
        # Получаем все критерии
        min_rate = context.user_data.get('min_rate', 7.0)
        sector = context.user_data.get('sector')
        limit = context.user_data.get('limit', 5)
        
        # Запускаем поиск в фоновом режиме
        asyncio.create_task(
            find_and_send_bonds(
                update.message, 
                min_rate=min_rate, 
                sector=sector, 
                limit=limit
            )
        )
        
        # Завершаем диалог
        return ConversationHandler.END
            
    except ValueError:
        await update.message.reply_text("❌ Пожалуйста, введите целое число от 1 до 10")
        return LIMIT
    except Exception as e:
        logger.error(f"Ошибка при поиске облигаций: {str(e)}", exc_info=True)
        await update.message.reply_text(f"⚠️ Произошла ошибка при поиске: {str(e)}")
        return ConversationHandler.END

async def cancel_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Поиск отменен")
    return ConversationHandler.END

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    # Обработчики команд
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("random", random_bond))
    
    # Обработчик поиска с использованием ConversationHandler
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("search", search_bonds)],
        states={
            SECTOR_SELECTION: [
                CallbackQueryHandler(handle_sector_selection, pattern="^sector_")
            ],
            MIN_RATE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_min_rate)
            ],
            LIMIT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_limit)
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel_search)],
    )
    
    app.add_handler(conv_handler)
    
    # Запуск бота
    app.run_polling()

if __name__ == '__main__':
    main()