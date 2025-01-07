import os
from pathlib import Path
from typing import Optional

from datavac.util.conf import CONFIG
from datavac.util.logging import logger
from datavac.util.util import import_modfunc


def get_db_connection_info() -> dict:
    """ Returns the connection information for the database

    If a replacement function is designated in the configuration (database.credentials.get_db_connection_info),
    it will be called.  Otherwise, the environment variables DATAVACUUM_DBSTRING and DATAVACUUM_DB_DRIVERNAME
    will be used, in concert with get_sslrootcert().

    The format is a dictionary with keys "Driver","Uid","Password","Server","Port","Database","sslargs".
    All those map to strings except for "sslargs".
    "sslargs" maps to a dictionary, either empty or containing the keys 'sslmode' and 'sslrootcert',
    each of which maps to a string, eg sslmode->'verify_full', sslrootcert->path to root certificate.
    The Driver string should be recognized by URL as a SQLAlchemy driver.

    Returns:
        dict: connection info
    """
    try:
        dotpath=CONFIG['database']['credentials']['get_db_connection_info']
    except KeyError:
        logger.debug("No database.credentials.get_db_connection_info configured, falling back on environment")
        connection_info=get_db_connection_info_from_environment()
    else:
        connection_info=import_modfunc(dotpath)()
    return connection_info

def get_db_connection_info_from_environment() -> dict:
    """ See get_db_connection_info, this is just the fallback-to-environment case. """
    dbstring=os.environ['DATAVACUUM_DBSTRING']
    connection_info=dict([[s.strip() for s in x.split("=")] for x in dbstring.split(";")])
    connection_info['Driver']=os.environ['DATAVACUUM_DB_DRIVERNAME']
    connection_info['sslargs']={'sslrootcert':sslrootcert,'sslmode':'verify-full'} \
        if (sslrootcert:=get_ssl_rootcert_for_db()) is not None else {}
    return connection_info

def get_ssl_rootcert_for_db() -> Optional[None]:
    """Returns the path to the SSL root certificate

    If a replacement function is designated in the configuration (database.credentials.get_ssl_rootcert_for_db),
    it will be called.  Otherwise, the environment variable DATAVACUUM_SSLROOTCERT will be used.

    Returns:
        Optional[None]: The path to the SSL root certificate, or None if not found
    """
    try:
        dotpath=CONFIG['database']['credentials']['get_ssl_rootcert_for_db']
    except KeyError:
        logger.debug("No database.credentials.get_ssl_rootcert_for_db configured, falling back on environment")
        pth=os.environ.get('DATAVACUUM_SSLROOTCERT',None)
    else:
        pth=import_modfunc(dotpath)()
    if pth is not None: assert Path(pth).exists(), f"SSL root certificate not found at {pth}"
    return pth