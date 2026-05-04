# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

This directory contains DHL international shipping order data in Excel format. Each file represents a shipment batch, typically organized by date and vehicle ("车"/truck).

## Data Structure

Files follow the naming pattern `DHL <date>.xlsx` or `<date> 第X台车<vehicle-id>.xlsx`.

### Primary order file (e.g., `DHL 4-30A.xlsx`)

**Sheet 1 — Order Lines** (main data, ~385 rows per batch):
| Column | Field |
|--------|-------|
| A | Currency (e.g., USD, EUR) |
| B | Reference number (shipper's order ref) |
| C | Tracking number (转单号) |
| D | Sub-order number (子单号, comma-separated if multiple) |
| E | Number of pieces (件数) |
| F | Chargeable weight (结算重, kg) |
| G | Product name (Chinese) |
| H | Total quantity |
| I | Destination country code (ISO 2-letter) |
| J | Destination postal code |
| K | Total declared value |
| L | Shipping channel code |
| M | Account number |
| N-Q | Product English name 1 + quantity |
| R-W | Repeat for up to 5 products per order |

**Sheet 2 — Account-Reference Mapping**: Maps account numbers to reference numbers.

**Sheet 3 — Country Accounts**: Destination country account info (account number, company name, country, contact email).

**Sheet 4 — Channel Mappings**: Shipping channel codes mapped to their descriptive service names (e.g., `HKDHL_V-快` → "Hong Kong DHL Express").

### Second vehicle file (e.g., `04-30 第二台车HW56.xlsx`)

Single sheet with columns: tracking number, item description (Chinese), pieces, weight (kg), quantity, declared value.

## Working with the data

The Python environment has numpy compatibility issues. Use the following pattern for reading Excel files:

```python
import zipfile, xml.etree.ElementTree as ET

z = zipfile.ZipFile("filename.xlsx")
ns = {'ns': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
# Read shared strings first: parse z.open('xl/sharedStrings.xml') with namespace ns
# Then parse individual sheets: z.open('xl/worksheets/sheet1.xml') etc.
# Cell references use shared string indices when attribute t="s"
```

Alternatively, first run `pip install --upgrade numpy openpyxl pandas` to fix the environment before using pandas/ openpyxl.
