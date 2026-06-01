"""ServiceNow -> Azure AI Search ingestion pipeline.

Estrae ticket chiusi da ServiceNow filtrati per resolver group e configuration
item, li redige, ne calcola gli embedding e li scrive in upsert idempotente su
un indice Azure AI Search esistente, usato come knowledge source da Copilot
Studio per l'help desk Oracle di secondo livello.
"""

__version__ = "1.0.0"
