"""Stock-take helpers.

Single home for the StockTakeRefType ↔ AdjustmentTarget conversion.

AUDIT RATIONALE
---------------
Both enums declare values named COIN_STOCK and OUNCE_STOCK, but the two
are different types in different modules with different lifecycles. Name
parity today does not guarantee semantic parity tomorrow — if someone
adds a new value to one enum without updating the other, we want a loud
KeyError at conversion time, not a silent miscategorization that posts
an adjustment against the wrong inventory table.

This module is therefore the ONLY place that translates between them,
and `test_stock_take_ref_type_mapping.py` asserts every value of
`StockTakeRefType` is present in the mapping.
"""
from app.models import AdjustmentTarget, StockTakeRefType


# Explicit table. No fallthrough, no name-based magic.
_REF_TYPE_TO_ADJUSTMENT_TARGET: dict[StockTakeRefType, AdjustmentTarget] = {
    StockTakeRefType.COIN_STOCK: AdjustmentTarget.COIN_STOCK,
    StockTakeRefType.OUNCE_STOCK: AdjustmentTarget.OUNCE_STOCK,
}


def to_adjustment_target(ref_type: StockTakeRefType) -> AdjustmentTarget:
    """Map a stock-take ref_type to the matching AdjustmentTarget.

    Raises KeyError if `ref_type` is not in the mapping — preferable to
    silently posting an adjustment against the wrong table. The
    completeness test guarantees this can only happen if a new enum
    value lands without a corresponding mapping update.
    """
    return _REF_TYPE_TO_ADJUSTMENT_TARGET[ref_type]
