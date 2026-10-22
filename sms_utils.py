"""Conservative SMS segment estimator: GSM-7 / UCS-2, including extension chars."""
GSM_BASIC = set(
    '@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ '
    '!"#¤%&\'()*+,-./0123456789:;<=>?¡'
    'ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿'
    'abcdefghijklmnopqrstuvwxyzäöñüà'
)
GSM_EXT = set('^{}\\[~]|€\f')


def estimate_sms(text):
    if all(c in GSM_BASIC or c in GSM_EXT for c in text):
        units=sum(2 if c in GSM_EXT else 1 for c in text)
        parts=1 if units<=160 else (units+152)//153
        return 'GSM-7',units,max(1,parts)
    units=len(text.encode('utf-16-le'))//2
    parts=1 if units<=70 else (units+66)//67
    return 'Unicode',units,max(1,parts)
