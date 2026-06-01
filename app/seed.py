"""
Run with:  python -m app.seed
"""
import asyncio
from decimal import Decimal

from app.config import settings
from app.core.pricing import KARAT_LABEL, generate_item_code
from app.core.security import hash_password
from app.db.session import async_session_factory, engine
from app.db.base import Base
from app.models import GoldRateHistory, Karat, Role, Settings, User
import app.models  # noqa: F401


SAMPLE_PRODUCTS = [
    ("Beirut Coil Bracelet", "بريسليت كويل بيروت", "Bracelets", Karat.K21, Decimal("18.42"), Decimal("18"), Decimal("35")),
    ("Classic Chain Necklace", "قلادة سلسلة كلاسيكية", "Necklaces", Karat.K18, Decimal("12.50"), Decimal("15"), Decimal("25")),
    ("Diamond Cut Ring", "خاتم قطع الماس", "Rings", Karat.K21, Decimal("5.30"), Decimal("20"), Decimal("40")),
    ("Gold Drop Earrings", "أقراط قطرة ذهبية", "Earrings", Karat.K18, Decimal("4.80"), Decimal("18"), Decimal("20")),
    ("Wide Cuff Bangle", "أسورة واسعة", "Bracelets", Karat.K24, Decimal("22.10"), Decimal("12"), Decimal("45")),
    ("Hoop Earrings Small", "أقراط حلقة صغيرة", "Earrings", Karat.K18, Decimal("3.20"), Decimal("18"), Decimal("15")),
    ("Figaro Chain Bracelet", "أسورة سلسلة فيغارو", "Bracelets", Karat.K21, Decimal("9.75"), Decimal("16"), Decimal("30")),
    ("Solitaire Ring", "خاتم سوليتير", "Rings", Karat.K18, Decimal("6.10"), Decimal("22"), Decimal("50")),
    ("Snake Chain Necklace", "قلادة سلسلة ثعبان", "Necklaces", Karat.K24, Decimal("15.80"), Decimal("14"), Decimal("35")),
    ("Charm Bracelet", "أسورة سحر", "Bracelets", Karat.K18, Decimal("8.40"), Decimal("17"), Decimal("28")),
]


async def seed():
    # Create tables if they don't exist (dev helper — in prod use Alembic)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with async_session_factory() as db:
        # Admin user
        from sqlalchemy import select
        existing_admin = (await db.execute(select(User).where(User.email == settings.seed_admin_email))).scalar_one_or_none()
        if not existing_admin:
            db.add(User(
                email=settings.seed_admin_email,
                name="Store Owner",
                password_hash=hash_password(settings.seed_admin_password),
                role=Role.ADMIN,
            ))
            print(f"Created admin: {settings.seed_admin_email}")

        # Cashier
        cashier_email = "cashier@fawazelnamel.com"
        existing_cashier = (await db.execute(select(User).where(User.email == cashier_email))).scalar_one_or_none()
        if not existing_cashier:
            db.add(User(
                email=cashier_email,
                name="Default Cashier",
                password_hash=hash_password("Cashier123!"),
                role=Role.CASHIER,
            ))
            print(f"Created cashier: {cashier_email}")

        # Settings singleton
        from app.models import Settings as SettingsModel
        existing_settings = (await db.execute(select(SettingsModel).where(SettingsModel.id == "singleton"))).scalar_one_or_none()
        if not existing_settings:
            db.add(SettingsModel(
                id="singleton",
                store_name="Fawaz El Namel",
                store_name_ar="فواز النمل",
                address="Hamra Street, Beirut, Lebanon",
                phone="+961 1 123 456",
                vat_number="VAT-12345",
                default_margin_pct=Decimal("15"),
                default_making_charge=Decimal("25"),
                vat_percent=Decimal("11"),
                lbp_exchange_rate=Decimal("89500"),
                receipt_footer="Thank you for shopping at Fawaz El Namel",
                gold_refresh_minutes=15,
            ))
            print("Created settings singleton")

        # Seed a default gold rate so the system has something to work with
        existing_rate = (await db.execute(select(GoldRateHistory).limit(1))).scalar_one_or_none()
        if not existing_rate:
            db.add(GoldRateHistory(rate_24k=Decimal("107.42"), source="seed"))
            print("Seeded initial gold rate: 107.42 USD/g")

        await db.commit()

        # Sample products
        from app.models import Product
        for name_en, name_ar, category, karat, weight, margin, making in SAMPLE_PRODUCTS:
            exists = (await db.execute(select(Product).where(Product.name_en == name_en))).scalar_one_or_none()
            if not exists:
                code = await generate_item_code(db, karat)
                db.add(Product(
                    code=code,
                    name_en=name_en,
                    name_ar=name_ar,
                    category=category,
                    karat=karat,
                    weight_grams=weight,
                    margin_percent=margin,
                    making_charge=making,
                    photos=[],
                ))
                await db.flush()
                print(f"  Created product: {code} — {name_en}")

        await db.commit()
        print("Seed complete.")


if __name__ == "__main__":
    asyncio.run(seed())
