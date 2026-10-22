"""Phone validation and lightweight international-number normalization."""
import re


def normalized_phone(raw: str, default_country: str = '46') -> str:
    digits = re.sub(r'[^\d+]', '', (raw or '').strip())
    if digits.startswith('00'):
        digits = '+' + digits[2:]
    if digits.startswith('+'):
        return '+' + re.sub(r'\D', '', digits)
    digits = re.sub(r'\D', '', digits)
    cc = re.sub(r'\D', '', default_country or '')
    if digits.startswith('0') and cc:
        return '+' + cc + digits.lstrip('0')
    return digits


def valid_phone(raw: str, default_country: str = '46') -> bool:
    val = normalized_phone(raw, default_country)
    return bool(re.fullmatch(r'\+?[0-9]{6,16}', val))
