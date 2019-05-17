import logging
from pathlib import Path

from tshistory.util import unilist, sqlfile


CREATEFILE = Path(__file__).parent / 'schema.sql'


class tsschema(object):
    namespace = 'tsh'

    def __init__(self, ns='tsh'):
        self.namespace = ns

    def create(self, engine):
        with engine.begin() as cn:
            cn.execute(f'drop schema if exists "{self.namespace}" cascade')
            cn.execute(f'drop schema if exists "{self.namespace}.timeserie" cascade')
            cn.execute(f'drop schema if exists "{self.namespace}.snapshot" cascade')
            cn.execute(f'create schema "{self.namespace}"')
            cn.execute(f'create schema "{self.namespace}.timeserie"')
            cn.execute(f'create schema "{self.namespace}.snapshot"')
            cn.execute(sqlfile(CREATEFILE, ns=self.namespace))
