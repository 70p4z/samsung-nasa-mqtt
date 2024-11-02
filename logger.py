import os

import logging
import logging.handlers
LOGSTEM = "samsung_nasa"
LOGLEVEL = os.environ.get('LOGLEVEL', 'INFO').upper()
LOGFORMAT = '%(asctime)s %(levelname)s %(threadName)s %(message)s'
logging.basicConfig(format=LOGFORMAT)
log = logging.getLogger(LOGSTEM)
log.setLevel(LOGLEVEL)
# add log rotation with max size
handler = logging.handlers.RotatingFileHandler("/tmp/"+LOGSTEM+".log", maxBytes=10000000, backupCount=1)
handler.setFormatter(logging.Formatter(LOGFORMAT))
log.addHandler(handler)