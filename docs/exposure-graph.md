# Structured exposure and paths

An edge may exist without a numeric exposure. Missing exposure remains `None`, never
zero. EIE-10 does not derive relationship strength from text mentions, observed returns
or EIE-9 impact estimates.

## Measurement convention

`ExposureMeasurement` stores a typed fraction with a source reference and measurement
reference time, plus either an exact point or lower/upper bounds. The canonical unit is
`FRACTION`: 12% is `0.12`. `exposure_from_percent` performs an explicit conversion;
passing `PERCENT` into a canonical measurement is rejected. Values must be finite and
immutable; booleans, NaN, infinity, reversed/empty ranges and future reference dates fail.

| Exposure type | Applicable relationship |
| --- | --- |
| `PORTFOLIO_WEIGHT` | ETF holds security/ETF |
| `OWNERSHIP_PERCENT`, `VOTING_POWER`, `ECONOMIC_INTEREST` | Parent or equity ownership |
| `REVENUE_SHARE`, `PURCHASE_SHARE` | Directional supplier/customer |
| `INDEX_WEIGHT` | Index membership |

Ordinary fractions lie in `[0,1]`. Signed portfolio exposure requires both the explicit
`SIGNED_PORTFOLIO` domain (bounded `[-100,100]`) and a provider configured to permit it.
Long-only is the default. The signed domain is not accepted for ownership or ordinary
index weights. The numerical cap is an ingestion bound, not a claim that a leveraged
holding has a particular economic interpretation.

“More than 10%” is `lower=0.10, lower_inclusive=False`, with unknown upper bound;
it is not the exact point 0.10. Unspecified bounds use the declared domain when composing
a derived ownership path. Exact zero is a reported value. Shares and market value live
in the separate `HoldingQuantity` contract with a reference time and explicit currency;
they do not silently become portfolio weights.

## ETF/index revisions and freshness

Reference time, relationship valid time and ingestion knowledge time are distinct. A
dated source snapshot has only exact point applicability unless carry is explicitly
configured. A new weight closes the earlier carried episode without assigning the new
weight to its earlier period. Partial compositions do not establish absence.

Provider alternatives retain their own measurements and references. Canonical resolution
selects one sourced measurement by categorical precedence; it does not sum or average
provider estimates. Disagreement is explicit. `estimated` and an optional
`stale_exposure_ns` policy produce descriptive flags. No opaque edge strength combines
classifications, supply, holdings, ownership and empirical response.

## Derived ownership paths

`compose_ownership` multiplies compatible forward `OWNERSHIP_PERCENT` fractions only.
For sourced A → B = 0.8 and B → C = 0.5, the path artifact reports 0.4 with method
`DERIVED_PATH_EXPOSURE_OWNERSHIP_PRODUCT` and exact ordered edge revision fingerprints.
It is a calculation attached to a path, not a new canonical A → C fact or total ownership.

Ranges multiply conservatively with explicit inclusive/exclusive bounds. Missing inputs
return unknown, including when another factor is zero. Known zero with all other inputs
known yields exact zero. Nonzero products that underflow are rejected rather than
masquerading as zero. Reverse ownership, supply revenue share, portfolio/index weight,
voting power and mixed types do not compose under this policy.

Traversal can request `derive_ownership=True`. Unsupported, missing or underflowing
compositions retain the path with `EXPOSURE_UNAVAILABLE`; they do not invalidate the
sourced relationships. Alternate paths are not summed: overlapping ownership, voting
structures, correlated supply chains and holding look-through need separately specified
aggregation policies. EIE-10 supplies no such aggregate and no expected return.
