import argparse
import functools
import os
import pickle
import sys
from datetime import datetime
from pathlib import Path
from typing import Union, Callable

import requests

from datavac.io.layout_params import LayoutParameters
from datavac.io.meta_reader import ensure_meas_group_sufficiency
from datavac.io.postgresql_binary_format import df_to_pgbin, pd_to_pg_converters
from prompt_toolkit import prompt
from sqlalchemy import text, Engine, create_engine, ForeignKey, UniqueConstraint, PrimaryKeyConstraint, \
    ForeignKeyConstraint, DOUBLE_PRECISION, delete, select, literal, union_all, insert
from sqlalchemy.dialects.postgresql import insert as pgsql_insert, BYTEA, TIMESTAMP
from sqlalchemy import INTEGER, VARCHAR, BOOLEAN, Column, Table, MetaData
import numpy as np
from sqlalchemy.engine import URL
import io
import pandas as pd

from datavac.util.conf import CONFIG
from datavac.util.logging import logger, time_it
from datavac.util.util import returner_context, import_modfunc

_CASC=dict(onupdate='CASCADE',ondelete='CASCADE')

_database:'PostgreSQLDatabase'=None
def get_database(cached=True,on_mismatch='raise',skip_establish=False) -> 'PostgreSQLDatabase':
    global _database
    assert os.environ['DATAVACUUM_DB_DRIVERNAME']=='postgresql', \
        "Must supply environment variable DATAVACUUM_DB_DRIVERNAME.  Only option at present is 'postgresql'"
    if not _database:
        _database=PostgreSQLDatabase(on_mismatch=on_mismatch,skip_establish=skip_establish)
    return _database

class Database:
    def get_data(self,meas_group,scalar_columns=None,include_sweeps=False,
                 unstack_headers=False,raw_only=False,**factors):
        raise NotImplementedError
    def get_factors(self,meas_group,factor_names,pre_filters={}):
        raise NotImplementedError

# TODO: Right now, a lot of PostgreSQL-specific functions are used by AlchemyDatabase
class AlchemyDatabase:

    engine:  Engine
    _metadata: MetaData

    def __init__(self, on_mismatch='raise', skip_establish=False):
        self._sslrootcert = os.environ.get('DATAVACUUM_DB_SSLROOTCERT',None)
        if self._sslrootcert and not Path(self._sslrootcert).exists():
            if (rootcerturl:=os.environ.get('DATAVACUUM_DB_SSLROOTCERT_DOWNLOAD',None)):
                logger.debug(f"{self._sslrootcert} doesn't exist; downloading...")
                with open(self._sslrootcert,'wb') as f:
                    f.write((res:=requests.get(rootcerturl,verify=False)).content)
        assert (self._sslrootcert is None or Path(self._sslrootcert).exists()),\
            f"SSL root cert {self._sslrootcert} does not exist."
        with time_it("Initializing Database took"):
            self._make_engine()
            self._init_metadata()
            if not skip_establish:
                self.establish_database(on_mismatch=on_mismatch)

    def _make_engine(self):
        connection_info=dict([[s.strip() for s in x.split("=")] for x in
                              os.environ['DATAVACUUM_DBSTRING'].split(";")])
        url=URL.create(
            drivername=os.environ["DATAVACUUM_DB_DRIVERNAME"],
            username=connection_info['Uid'],
            password=connection_info['Password'],
            host=connection_info['Server'],
            port=int(connection_info['Port']),
            database=connection_info['Database'],
        )
        # For synchronous
        ssl_args = {'sslmode':'verify-full',
                    'sslrootcert': self._sslrootcert}\
            if self._sslrootcert else {}

        self.engine=create_engine(url, connect_args=ssl_args, pool_recycle=60)

    def _init_metadata(self):
        self._metadata = MetaData(schema=CONFIG['database']['schema_names']['internal'])
        with self.engine.connect() as conn:
            self._metadata.reflect(conn)

    def clear_database(self, only_tables=None, conn=None):
        removed_tables=[]
        with (returner_context(conn) if conn else self.engine.begin()) as conn:
            # MetaData has a drop_all, but I had issues with references and needed to explicitly DROP ... CASCADE
            # So implementing here
            for table in list(self._metadata.tables.values()):
                if only_tables and table not in only_tables: continue
                conn.execute(text(f'DROP TABLE {table.schema}."{table.name}" CASCADE;'))
                removed_tables.append(table)
                self._metadata.remove(table)
        if only_tables:
            assert list(sorted([(t.schema,t.name) for t in removed_tables]))==\
                   list(sorted([(t.schema,t.name) for t in only_tables])),\
                f"Trouble removing {[t.name for t in only_tables]}"

    @property
    def int_schema(self):
        return CONFIG['database']['schema_names']['internal']
    @property
    def _mattab(self) -> Table:
        return self._metadata.tables[f"{self.int_schema}.Materials"]
    @property
    def _loadtab(self) -> Table:
        return self._metadata.tables[f"{self.int_schema}.Loads"]
    @property
    def _rextab(self) -> Table:
        return self._metadata.tables[f"{self.int_schema}.ReExtract"]
    @property
    def _reatab(self) -> Table:
        return self._metadata.tables[f"{self.int_schema}.ReAnalyze"]
    @property
    def _masktab(self) -> Table:
        return self._metadata.tables[f"{self.int_schema}.Masks"]
    @property
    def _diemtab(self) -> Table:
        return self._metadata.tables[f"{self.int_schema}.Dies"]
    def _mgt(self,mg,wh) -> Table:
        return self._metadata.tables.get(f"{self.int_schema}.{wh.capitalize()} -- {mg}",None)
    def _hat(self,an) -> Table:
        return self._metadata.tables.get(f"{self.int_schema}.Analysis -- {an}",None)


        #@staticmethod
        #def df_to_alchemy_columns(table: pd.DataFrame):
        #    type_mapping={
        #        'int64':INTEGER,'Int64':INTEGER,
        #        'int32':INTEGER,'Int32':INTEGER,
        #        'string':VARCHAR,
        #        'float32':DOUBLE_PRECISION,'Float32':DOUBLE_PRECISION,
        #        'float64':DOUBLE_PRECISION,'Float64':DOUBLE_PRECISION,
        #        'bool':Boolean,'boolean':Boolean}
        #    return [Column(c,type_mapping[str(cdtype)],primary_key=(c=='LotWafer')) for c,cdtype in table.dtypes.items()]

class PostgreSQLDatabase(AlchemyDatabase):

    pd_to_sql_types={
        'int64':INTEGER,'Int64':INTEGER,
        'int32':INTEGER,'Int32':INTEGER,
        'string':VARCHAR,
        'str':VARCHAR,
        'float32':DOUBLE_PRECISION,'Float32':DOUBLE_PRECISION,
        'float64':DOUBLE_PRECISION,'Float64':DOUBLE_PRECISION,
        'bool':BOOLEAN,'boolean':BOOLEAN}
    sql_to_pd_types={
        INTEGER:'Int32',
        VARCHAR:'string',
        BOOLEAN: 'bool',
        DOUBLE_PRECISION:'float64'
    }

    def establish_database(self, on_mismatch='raise'):
        layout_params=LayoutParameters()

        with self.engine.connect() as conn:
            make_schemas=" ".join([f"CREATE SCHEMA IF NOT EXISTS {schema};"
                                   for schema in CONFIG['database']['schema_names'].values()])
            conn.execute(text(make_schemas+f"SET SEARCH_PATH={self.int_schema};"))

            if self.establish_mask_tables(conn, on_mismatch=on_mismatch):
                self.update_mask_info(conn)

            # Always raise if mismatch on these core tables because
            # there are foreign keys to them that will be lost if they are recreated

            # Materials table
            matscheme=CONFIG['database']['materials']
            self._ensure_table_exists(conn,self.int_schema,'Materials',
                        Column('matid',INTEGER,primary_key=True,autoincrement=True),
                        *[Column(name,VARCHAR,nullable=False) for name in matscheme['info_columns'] if name!='Mask'],
                        Column(matscheme['full_name'],VARCHAR,unique=True,nullable=False),
                        Column('Mask',VARCHAR,ForeignKey("Masks.Mask",name='fk_mask',**_CASC),nullable=False),
                        Column('date_user_changed',TIMESTAMP,nullable=False),
                        on_mismatch='raise')

            # Loads table
            loadtab=self._ensure_table_exists(conn,self.int_schema,'Loads',
                      Column('loadid',INTEGER,primary_key=True,autoincrement=True),
                      Column('matid',INTEGER,ForeignKey("Materials.matid",**_CASC),nullable=False),
                      Column('MeasGroup',VARCHAR,nullable=False),
                      UniqueConstraint('matid','MeasGroup'),
                      on_mismatch='raise')

            # Data that's been killed from Meas/Extr
            rextab=self._ensure_table_exists(conn,self.int_schema,'ReExtract',
                      Column('matid',INTEGER,ForeignKey("Materials.matid",**_CASC),nullable=False),
                      Column('MeasGroup',VARCHAR,nullable=False),
                      Column('full_reload',BOOLEAN,nullable=False),
                      UniqueConstraint('matid','MeasGroup'),
                      on_mismatch='raise')

            reatab=self._ensure_table_exists(conn,self.int_schema,'ReAnalyze',
                      Column('matid',INTEGER,ForeignKey("Materials.matid",**_CASC),nullable=False),
                      Column('analysis',VARCHAR,nullable=False),
                      UniqueConstraint('matid','analysis'),
                      on_mismatch='raise')

            conn.commit()

            # TODO: when moving layout params into main config, this loop should go over CONFIG['measurement_groups']
            for mg in layout_params._tables_by_meas:
                if mg not in CONFIG['measurement_groups']: continue
                self.establish_layout_parameters(layout_params,mg,conn,on_mismatch=on_mismatch)
                conn.commit()
            for mg in CONFIG['measurement_groups']:
                self.establish_measurement_group_tables(mg,conn,on_mismatch=on_mismatch)
                conn.commit()
            for an in CONFIG['higher_analyses']:
                self.establish_higher_analysis_tables(an,conn,on_mismatch=on_mismatch)
                conn.commit()

    def establish_mask_tables(self,conn, on_mismatch='raise'):
        needs_update=False
        def yes_needs_update():
            nonlocal needs_update
            needs_update=True
        self._ensure_table_exists(conn,self.int_schema,f'Masks',
                          Column('Mask',VARCHAR,nullable=False,unique=True,primary_key=True),
                          Column('info_pickle',BYTEA,nullable=False),
                          on_mismatch=on_mismatch,on_init=yes_needs_update)
        self._ensure_table_exists(conn,self.int_schema,f'Dies',
                          Column('dieid',INTEGER,autoincrement=True,nullable=False,primary_key=True),
                          Column('Mask',VARCHAR,ForeignKey("Masks.Mask",**_CASC),nullable=False,index=True),
                          Column('DieXY',VARCHAR,nullable=False,index=True),
                          Column('DieRadius [mm]',INTEGER,nullable=False),
                          Column('DieCenterX [mm]',DOUBLE_PRECISION,nullable=False),
                          Column('DieCenterY [mm]',DOUBLE_PRECISION,nullable=False),
                          UniqueConstraint('Mask','DieXY'),
                          on_mismatch=on_mismatch,on_init=yes_needs_update)
        return needs_update

    def ensure_die_constraint(self,conn):
        #tab=self._mgt(mg,'Meas').name
        res=conn.execute(
            text("SELECT table_name FROM information_schema.table_constraints"
                 f" WHERE table_schema='{self.int_schema}' AND "
                 " constraint_name='fk_dieid';"))
        constrained_tabs=[x[0] for x in res]
        unconstrained_tabs=[]
        for mg in list(CONFIG['measurement_groups']):
            if (tab:=self._mgt(mg,'Meas')) is not None:
                if tab.name not in constrained_tabs:
                    unconstrained_tabs.append(tab.name)
        for an in list(CONFIG['higher_analyses']):
            if (tab:=self._hat(an)) is not None:
                if tab.name not in constrained_tabs:
                    unconstrained_tabs.append(tab.name)
        for tab in unconstrained_tabs:
            conn.execute(text(f'ALTER TABLE {self.int_schema}."{tab}"' \
                              f' ADD CONSTRAINT "fk_dieid" FOREIGN KEY ("dieid")' \
                              f' REFERENCES {self.int_schema}."Dies" ("dieid") ON DELETE CASCADE;'))

    def update_mask_info(self,conn):
        """ Warning, this will currently wipe any data because I haven't unreferenced keys"""

        previous_masktab=pd.read_sql(select(*self._masktab.columns),conn)
        #######Column('Mask',VARCHAR,ForeignKey("Masks.Mask",name='fk_mask',**_CASC),nullable=False),
        diemdf=[]
        for mask,info in CONFIG['diemaps'].items():
            dbdf,to_pickle=import_modfunc(info['generator'])(**info['args'])
            diemdf.append(dbdf.assign(Mask=mask)[['Mask','DieXY','DieRadius [mm]','DieCenterX [mm]','DieCenterY [mm]']])
            update_info=dict(Mask=mask,info_pickle=pickle.dumps(to_pickle))
            conn.execute(pgsql_insert(self._masktab).values(**update_info)\
                         .on_conflict_do_update(index_elements=['Mask'],set_=update_info))

        diemdf=pd.concat(diemdf).reset_index(drop=True).reset_index(drop=False)
        previous_dietab=pd.read_sql(select(*self._diemtab.columns),conn).reset_index(drop=False)
        # This checks that nothing has changed in the previous table
        # very important to check that because all the measured data is only associated with a die index,
        # so if we accidentally change the die index, even by uploading the tables in a different order...
        # poof all the old data is now associated with the wrong dies or even wrong masks!!
        assert len(previous_dietab.merge(diemdf))==len(previous_dietab),\
            "Can't add to die tables without messing up existing dies"
        self._upload_csv(diemdf.iloc[len(previous_dietab):],conn,self.int_schema,'Dies')

        self.ensure_die_constraint(conn)

    def get_mask_info(self,mask):
        with self.engine.connect() as conn:
            res=conn.execute(select(self._masktab.c.info_pickle).where(self._masktab.c.Mask==mask)).all()
        assert len(res)==1, f"Couldn't get info from database about mask {mask}"
        # Must ensure restricted write access to DB since this allows arbitrary code execution
        return pickle.loads(res[0][0])

    def establish_layout_parameters(self,layout_params, measurement_group, conn, on_mismatch='raise'):
        mg, df=measurement_group, layout_params._tables_by_meas[measurement_group]
        assert len(df), f"Empty layout param table for {mg}"
        assert df.index.name=='Structure'
        tabname=f"Layout -- {mg}"

        def replace_callback():
            logger.warning(f'Mismatch in layout params for "{measurement_group}", re-creating table')
            if f"{self.int_schema}.Meas -- {mg}" in self._metadata.tables:
                conn.execute(text(f'ALTER TABLE {self.int_schema}."Meas -- {mg}" '
                                  f'DROP CONSTRAINT IF EXISTS "fk_struct -- {mg}";'))
            self.clear_database(only_tables=[tabname],conn=conn)
        def initialize_callback():
            self.update_layout_parameters(layout_params,mg,conn)

        cols=[Column('Structure',VARCHAR,primary_key=True),
              *[Column(k,self.pd_to_sql_types[str(dtype)]) for k,dtype in df.dtypes.items()]]
        self._ensure_table_exists(conn,self.int_schema,f'Layout -- {mg}',*cols,
                                  on_mismatch=on_mismatch, on_init=initialize_callback)


    def _upload_binary(self, df, conn, schema, table, override_converters={}):
        with time_it(f"Conversion to binary for {table}",.1):
            bio=df_to_pgbin(df, override_converters=override_converters)
            #print("BIO len:",len(bio.read()))
            #bio.seek(0)
            #print(bio.read().hex())
            #bio.seek(0)

        with time_it(f"Upload of binary for {table}",.1):
            with conn.connection.cursor() as cur:
                cur.copy_expert(f'COPY {schema}."{table}" FROM STDIN BINARY',bio)
        ##### TEMPORARILY REMOVING DB-API COMMIT
        #conn.connection.commit()

    def _upload_csv(self, df, conn, schema, table):
        with time_it(f"Conversion to csv for {table}",.1):
            output = io.StringIO()
            df.replace([np.inf,-np.inf],np.nan).to_csv(output, sep='|', header=False, index=False)
            #print("CSV len",len(output.getvalue().encode('utf-8')))
            #output.seek(0)
            #print(output.read())
            output.seek(0)
        with time_it(f"Upload of csv for {table}",.1):
            with conn.connection.cursor() as cur:
                cur.copy_expert(f'COPY {schema+"." if schema else ""}"{table}" FROM STDIN WITH DELIMITER \'|\' NULL \'\'',file=output)
        ##### TEMPORARILY REMOVING DB-API COMMIT
        # conn.connection.commit()

    def dump_extractions(self, measurement_group, conn):
        if (extr_tab:=self._mgt(measurement_group,'extr')) is None: return
        if (meas_tab:=self._mgt(measurement_group,'meas')) is None: return
        fullname=CONFIG['database']['materials']['full_name']
        conn.execute(
            pgsql_insert(self._rextab)\
                .from_select(["matid","MeasGroup",'full_reload'],
                    select(self._loadtab.c.matid,literal(measurement_group),literal(False))\
                           .select_from(extr_tab.join(meas_tab).join(self._loadtab))\
                           .distinct())\
                .on_conflict_do_nothing())
        conn.execute(delete(extr_tab))

    def dump_measurements(self, measurement_group, conn):
        try:
            meas_tab=self._mgt(measurement_group,'meas')
        except KeyError:
            return
        if meas_tab is not None:
            conn.execute(
                pgsql_insert(self._rextab) \
                    .from_select(["matid","MeasGroup",'full_reload'],
                                 select(self._loadtab.c.matid,literal(measurement_group),literal(True)) \
                                 .select_from(meas_tab.join(self._loadtab)) \
                                 .distinct()) \
                    .on_conflict_do_nothing())
            conn.execute(delete(meas_tab))

    def dump_higher_analysis(self, analysis, conn):
        if (an_tab:=self._hat(analysis)) is None: return
        conn.execute(
            pgsql_insert(self._reatab) \
                .from_select(["matid","analysis"],
                             select(self._loadtab.c.matid,literal(analysis)) \
                             .select_from(an_tab.join(self._loadtab)) \
                             .distinct()) \
                .on_conflict_do_nothing())
        conn.execute(delete(an_tab))

    def update_layout_parameters(self, layout_params, measurement_group, conn, dump_extractions=True):
        self.establish_layout_parameters(layout_params,measurement_group,conn, on_mismatch='replace')
        tab=self._metadata.tables[f'{self.int_schema}.Layout -- {measurement_group}']
        mg=measurement_group

        conn.execute(text(f'CREATE TEMP TABLE tmplay (LIKE {self.int_schema}."Layout -- {measurement_group}");'))
        self._upload_csv(layout_params._tables_by_meas[measurement_group].reset_index(),
                         conn, None, 'tmplay')
        # https://dba.stackexchange.com/a/72642
        if conn.execute(text(
            f'''SELECT CASE WHEN EXISTS (TABLE {self.int_schema}."Layout -- {measurement_group}" EXCEPT TABLE tmplay)
              OR EXISTS (TABLE tmplay EXCEPT TABLE {self.int_schema}."Layout -- {measurement_group}")
            THEN 'different' ELSE 'same' END AS result ;''')).all()[0][0] == 'same':
            logger.debug(f"Layout parameters unchanged for {measurement_group}")
        else:
            logger.debug(f"Layout parameters changed for {measurement_group}, updating")
            if dump_extractions:
                self.dump_extractions(measurement_group,conn)
            if self._mgt(measurement_group,'meas') is not None:
                conn.execute(text(f'ALTER TABLE {self.int_schema}."Meas -- {measurement_group}"'\
                                  f' DROP CONSTRAINT IF EXISTS "fk_struct -- {mg}";'))
            conn.execute(delete(tab))
            conn.execute(text(f'INSERT INTO {tab.schema}."{tab.name}" SELECT * from tmplay;'))
            if (self._mgt(measurement_group,'meas') is not None):
                conn.execute(text(f'ALTER TABLE {self.int_schema}."Meas -- {measurement_group}"' \
                                  f' ADD CONSTRAINT "fk_struct -- {mg}" FOREIGN KEY ("Structure")' \
                                  f' REFERENCES {self.int_schema}."{tab.name}" ("Structure") ON DELETE CASCADE;'))

        conn.execute(text(f'DROP TABLE tmplay;'))
        conn.commit()


    def establish_measurement_group_tables(self,measurement_group,conn, on_mismatch='raise'):
        mg, mg_info = measurement_group, CONFIG['measurement_groups'][measurement_group]
        layout_params=LayoutParameters()

        # Meas table
        def meas_replacement_callback(conn,schema,table_name,*args,**kwargs):
            # If replacing, need to drop the other tables for this meas group as well
            # since I don't want to manually recreate foreign keys
            self.dump_measurements(mg,conn)
            tabs=[self._metadata.tables.get(f'{schema}.{w} -- {mg}',None) for w in ['Meas','Extr','Sweep']]
            tabs=[tab for tab in tabs if tab is not None]
            logger.warning(f"Dumping and replacing {[tab.name for tab in tabs]}")
            self.clear_database(only_tables=tabs,conn=conn)
        self._ensure_table_exists(conn,self.int_schema,f'Meas -- {mg}',
            Column('loadid',INTEGER,ForeignKey("Loads.loadid",**_CASC),nullable=False),
            Column('measid',INTEGER,nullable=False),
            Column('Structure',VARCHAR,ForeignKey(f'Layout -- {mg}.Structure',
                                                  name=f'fk_struct -- {mg}',**_CASC),nullable=False),
            Column('dieid',INTEGER,ForeignKey(f'Dies.dieid',name='fk_dieid',**_CASC),nullable=False),
            Column('rawgroup',INTEGER,nullable=False),
            *[Column(k,self.pd_to_sql_types[dtype]) for k,dtype in mg_info['meas_columns'].items()],
            PrimaryKeyConstraint('loadid','measid'),
            on_mismatch=(meas_replacement_callback if on_mismatch=='replace' else on_mismatch))

        # Extr table
        def extr_replacement_callback(conn,schema,table_name,*args,**kwargs):
            logger.warning(f"Dumping and replacing {table_name}")
            tab=self._metadata.tables[f'{schema}.{table_name}']
            self.dump_extractions(mg,conn)
            self.clear_database(only_tables=[tab],conn=conn)
        self._ensure_table_exists(conn,self.int_schema,f'Extr -- {mg}',
            Column('loadid',INTEGER,nullable=False),
            Column('measid',INTEGER,nullable=False),
            PrimaryKeyConstraint('loadid','measid'),
            ForeignKeyConstraint(columns=['loadid','measid'],**_CASC,
                                 refcolumns=[f"Meas -- {mg}.loadid",f"Meas -- {mg}.measid",]),
            *[Column(k,self.pd_to_sql_types[dtype]) for k,dtype in mg_info['analysis_columns'].items()],
            on_mismatch=(extr_replacement_callback if on_mismatch=='replace' else on_mismatch))

        # Sweep table
        def sweep_replacement_callback(conn,schema,table_name,*args,**kwargs):
            logger.warning(f"Dumping and replacing {table_name}")
            tab=self._metadata.tables[f'{schema}.{table_name}']
            self.dump_measurements(mg,conn)
            self.clear_database(only_tables=[tab],conn=conn)
        self._ensure_table_exists(conn,self.int_schema,f'Sweep -- {mg}',
            Column('loadid',INTEGER,nullable=False),
            Column('measid',INTEGER,nullable=False),
            Column('sweep',BYTEA,nullable=False),
            Column('header',VARCHAR,nullable=False),
            PrimaryKeyConstraint('loadid','measid','header'),
            ForeignKeyConstraint(columns=['loadid','measid'],**_CASC,
                                 refcolumns=[f"Meas -- {mg}.loadid",f"Meas -- {mg}.measid",]),
            on_mismatch=on_mismatch)

        # TODO: Replace this with SQLAlchemy select like in get_where
        view_cols=[
            CONFIG['database']['materials']['full_name'],
            *(f'Materials"."{i}' for i in CONFIG['database']['materials']['info_columns']),
            f'Meas -- {mg}"."Structure',
            f'Dies"."DieXY',f'Dies"."DieRadius [mm]',
            *mg_info['analysis_columns'].keys(),
            *mg_info['meas_columns'].keys(),
            *([c for c in layout_params._tables_by_meas[mg].columns if not c.startswith("PAD")]
                  if mg in layout_params._tables_by_meas else [])
        ]
        view_cols=",".join([f'"{c}"' for c in view_cols])
        conn.execute(text(f'DROP VIEW IF EXISTS jmp."{mg}"; CREATE VIEW jmp."{mg}" AS SELECT {view_cols} from '\
            f'"Extr -- {mg}" '\
            f'JOIN "Meas -- {mg}" ON "Extr -- {mg}".loadid="Meas -- {mg}".loadid '\
                               f'AND "Extr -- {mg}".measid="Meas -- {mg}".measid '\
            f'JOIN "Layout -- {mg}" ON "Meas -- {mg}"."Structure"="Layout -- {mg}"."Structure" '
            f'JOIN "Loads" ON "Loads".loadid="Meas -- {mg}".loadid ' \
            f'JOIN "Dies" ON "Meas -- {mg}".dieid="Dies".dieid '\
            f'JOIN "Materials" ON "Loads".matid="Materials".matid;'))

    def establish_higher_analysis_tables(self,analysis, conn, on_mismatch='raise'):
        reqlids=[Column(f'loadid - {mg}',INTEGER,ForeignKey(self._loadtab.c.loadid,**_CASC),nullable=False,index=True)
                 for mg in CONFIG.higher_analyses[analysis]['required_dependencies']]
        attlids=[Column(f'loadid - {mg}',INTEGER,ForeignKey(self._loadtab.c.loadid,**_CASC),nullable=True,index=True)
                 for mg in CONFIG.higher_analyses[analysis].get('attempt_dependencies',{})]
        def replacement_callback(conn,schema,table_name,*args,**kwargs):
            logger.warning(f"Dumping and replacing {table_name}")
            tab=self._metadata.tables[f'{schema}.{table_name}']
            self.dump_higher_analysis(analysis,conn)
            self.clear_database(only_tables=[tab],conn=conn)
        self._ensure_table_exists(conn,self.int_schema,f'Analysis -- {analysis}',
                  *reqlids,*attlids,
                  Column('dieid',INTEGER,ForeignKey(f'Dies.dieid',name='fk_dieid',**_CASC),nullable=False),
                  *[Column(k,self.pd_to_sql_types[dtype]) for k,dtype
                        in CONFIG.higher_analyses[analysis]['analysis_columns'].items()],
                  on_mismatch=(replacement_callback if on_mismatch=='replace' else on_mismatch))

    def _ensure_table_exists(self, conn, schema, table_name, *args,
                             on_mismatch:Union[str,Callable]='raise',
                             on_init: Callable= (lambda : None)):
        should_be_columns=[x for x in args if isinstance(x,Column)]
        if (tab:=self._metadata.tables.get(f'{schema}.{table_name}',None)) is not None:
            try:
                assert [c.name for c in tab.columns]==[c.name for c in should_be_columns]
                assert [c.type.__class__ for c in tab.columns]==[c.type.__class__ for c in should_be_columns]
            except AssertionError:
                logger.warning(f"Column mismatch in {tab.name} (note, only name and type class are checked)")
                logger.warning(f"Currently in DB: {[(c.name,c.type.__class__.__name__) for c in tab.columns]}")
                logger.warning(f"Should be in DB: {[(c.name,c.type.__class__.__name__) for c in should_be_columns]}")
                if on_mismatch=='raise':
                    raise
                elif on_mismatch=='replace':
                    logger.warning(f"Replacing {tab.name}")
                    self.clear_database(only_tables=[tab],conn=conn)
                elif callable(on_mismatch):
                    on_mismatch(conn,schema,table_name,*args)
                else:
                    raise Exception(f"Can't interpret on_mismatch={on_mismatch}")
        if (tab:=self._metadata.tables.get(f'{schema}.{table_name}',None)) is None:
            tab=Table(table_name,self._metadata,*args)
            tab.create(conn)
            on_init()
        return tab

    def drop_material(self, material_info, conn, only_meas_group=None):
        """Does not commit, so transaction will continue to have lock on Materials table."""
        fullmatname_col=CONFIG['database']['materials']['full_name']
        if only_meas_group is None:
            statement=delete(self._mattab)\
                             .where(self._mattab.c[fullmatname_col]==material_info[fullmatname_col])\
                             .returning(self._mattab.c.date_user_changed)
            #print(conn.execute(text("EXPLAIN (ANALYZE,BUFFERS) "+str(statement.compile(compile_kwargs={'literal_binds':True})))).all())
            res=conn.execute(delete(self._mattab)\
                             .where(self._mattab.c[fullmatname_col]==material_info[fullmatname_col])\
                             .returning(self._mattab.c.date_user_changed)).all()
            if len(res): return res[0][0]
        else:
            conn.execute(delete(self._loadtab) \
                             .where(self._mattab.c[fullmatname_col]==material_info[fullmatname_col]) \
                             .where(self._mattab.c.matid==self._loadtab.c.matid)\
                             .where(self._loadtab.c.MeasGroup==only_meas_group))

    def enter_material(self, conn, user_called=True, **material_info):
        """Does not commit, so transaction will continue to have lock on Materials table."""
        fullmatname_col=CONFIG['database']['materials']['full_name']
        if not user_called:
            res=conn.execute(select(self._mattab)\
                             .where(self._mattab.c[fullmatname_col]==material_info[fullmatname_col])).all()
            if len(res):
                matid=dict(zip([c.name for c in self._mattab.columns],res[0]))['matid']
                # TODO: Could put a check here that the rest of material_info is accurate...
            else:
                raise Exception(f"While regenerating, ran across unrecognized material {material_info[fullmatname_col]}")
        else:
            update_info=material_info.copy()
            update_info.update(date_user_changed=datetime.now())
            matid=conn.execute(pgsql_insert(self._mattab)\
                               .values(**update_info)\
                               .on_conflict_do_update(index_elements=[fullmatname_col],set_=update_info)\
                               .returning(self._mattab.c.matid))\
                        .all()[0][0]
        return matid

    def push_data(self, material_info, data_by_meas_group:dict,
                  clear_all_from_material=True, user_called=True, re_extraction=False):
        """
        Notes
        -----
        `re_extraction=True` should only be used by internal code healing the database
        (ie after a table has been dropped). If `re_extraction=True`, `push_data` will
        make no effort to clear out prior data, so abuse of this can result in uniqueness
        violation errors.
        """
        fullmatname_col=CONFIG['database']['materials']['full_name']
        assert not (user_called and re_extraction), "Re-extraction is not a user-update"

        with self.engine.connect() as conn:
            conn.execute(text(f"SET SEARCH_PATH={self.int_schema};"))

            # If clear material, then drop this material from the database before beginning
            date_user_changed=None
            if clear_all_from_material:
                assert not re_extraction, "Doesn't make sense to clear material when re-extracting"
                with time_it("Dropping all from material"):
                    date_user_changed=self.drop_material(material_info, conn)

            # Now ensure this material is in the database
            matid=self.enter_material(conn,**material_info, user_called=user_called,
                                      date_user_changed=date_user_changed)

            # Invalidate the relevant analyses
            insrt=[]
            analyses=CONFIG.get_dependent_analyses(list(data_by_meas_group.keys()))
            if len(analyses):
                for an in analyses:
                    insrt.append(
                        str(pgsql_insert(self._reatab) \
                            .values(matid=matid,analysis=an)\
                            .compile(compile_kwargs={'literal_binds':True})))
                conn.execute(text("; ".join(insrt)))


            # For each meas_group
            collected_loadids={}
            for meas_group, mt_or_df in data_by_meas_group.items():

                if not re_extraction:
                    # Drop any previous loads from the Loads table
                    # (if clear_material, this is already handled by drop_material above)
                    if not clear_all_from_material:
                        self.drop_material(material_info, conn, only_meas_group=meas_group)

                    # Put an entry into the Loads table and get the loadid
                    loadid=conn.execute(pgsql_insert(self._loadtab)\
                                       .values(matid=matid,MeasGroup=meas_group)\
                                       .returning(self._loadtab.c.loadid))\
                                .all()[0][0]

                #print(type(mt_or_df),isinstance(mt_or_df, MeasurementTable))
                from datavac.io.measurement_table import MeasurementTable
                if isinstance(mt_or_df, MeasurementTable):
                    analysis_cols=list(CONFIG['measurement_groups'][meas_group]['analysis_columns'])
                    meas_cols=list(CONFIG['measurement_groups'][meas_group]['meas_columns'])

                    df=mt_or_df._dataframe

                    if not re_extraction:
                        # Upload the measurement list
                        with time_it(f"Meas table {meas_group} altogether"):
                            mask=material_info['Mask']
                            diem=pd.DataFrame.from_records(conn.execute(select(self._diemtab.c.DieXY,self._diemtab.c.dieid)\
                                            .where(self._diemtab.c.Mask==mask)).all(),columns=['DieXY','dieid'])
                            df2=df.reset_index() \
                                .assign(loadid=loadid,Mask=mask).rename(columns={'index':'measid'}) \
                                [['loadid','measid','Structure','DieXY','rawgroup',*meas_cols]].merge(diem,how='left',on='DieXY')\
                                [['loadid','measid','Structure','dieid','rawgroup',*meas_cols]]
                            self._upload_csv(df2,conn,self.int_schema,self._mgt(meas_group,'meas').name)

                        # Upload the raw sweep
                        self._upload_binary(
                            df[mt_or_df.headers]
                                .stack().reset_index()
                                .assign(loadid=loadid).rename(columns={'level_0':'measid','level_1':'header',0:'sweep'}) \
                                [['loadid','measid','sweep','header']],
                            conn,self.int_schema,self._mgt(meas_group,'sweep').name,
                            override_converters={'sweep':lambda s: s.tobytes(),'header':pd_to_pg_converters['STRING']}
                        )

                    # Upload the extracted values
                    if not re_extraction:
                        df=df.assign(loadid=loadid).reset_index().rename(columns={'index':'measid'})
                    assert len(ulid:=df['loadid'].unique())==1
                    try:
                        df=df[['loadid','measid',*analysis_cols]]
                    except KeyError as e:
                        logger.warning(f"Missing columns: {str(e)}")
                        logger.warning(f"Present columns are {list(df.columns)}")
                        raise e
                    loadid=int(ulid[0])
                    try:
                        self._upload_csv(
                            df,
                            conn,self.int_schema,self._mgt(meas_group,'extr').name
                        )
                    except Exception as e:
                        print("OOPS")
                        raise e

                    # If we've succeeded thus far, we can drop this matid, MeasGroup from the refreshes table
                    dstat=delete(self._rextab)\
                        .where(self._rextab.c.MeasGroup==meas_group)\
                        .where(self._rextab.c.matid==self._loadtab.c.matid) \
                        .where(self._loadtab.c.loadid==loadid)
                    if re_extraction:
                        dstat=dstat.where(self._rextab.c.full_reload==False)
                    #print(dstat.compile())
                    conn.execute(dstat)

                    collected_loadids[meas_group]=loadid

            self.perform_analyses(conn, analyses,
                                  precollected_data_by_meas_group=data_by_meas_group,
                                  precollected_loadids=collected_loadids,
                                  precollected_matid=matid)
            conn.commit()
            logger.debug(f"Completed all tasks for {str(material_info)}")

    def perform_analyses(self, conn, analyses,
                         precollected_data_by_meas_group={}, precollected_loadids={}, precollected_matid=None):
        mg_to_data=precollected_data_by_meas_group
        matid=precollected_matid
        mask=conn.execute(select(self._mattab.c.Mask).where(self._mattab.c.matid==matid)).all()[0][0]
        diem=pd.DataFrame.from_records(conn.execute(
            select(self._diemtab.c.DieXY,self._diemtab.c.dieid) \
                .where(self._diemtab.c.Mask==mask)).all(),columns=['DieXY','dieid'])
        for an in analyses:
            logger.debug(f"Running analysis: {an}")
            df=import_modfunc(CONFIG.higher_analyses[an]['analysis_func'])(
                #**{v: mg_to_data[k].scalar_table_with_layout_params() for k,v in
                **{v: mg_to_data[k] for k,v in
                   CONFIG.higher_analyses[an]['required_dependencies'].items()},
                #**{v: mg_to_data.get(k,None).scalar_table_with_layout_params() for k,v in
                **{v: mg_to_data.get(k,None) for k,v in
                   CONFIG.higher_analyses[an].get('attempt_dependencies',{}).items()})

            loadids=dict(
                **{f'loadid - {mg}':precollected_loadids[mg] for mg in
                   CONFIG.higher_analyses[an]['required_dependencies']}, \
                **{f'loadid - {mg}':precollected_loadids.get(mg,None) for mg in
                   CONFIG.higher_analyses[an].get('attempt_dependencies',{})})


            #if not re_extraction:
            #    # Upload the measurement list
            #    with time_it("Meas table altogether"):
            #        mask=material_info['Mask']
            #        diem=pd.DataFrame.from_records(conn.execute(select(self._diemtab.c.DieXY,self._diemtab.c.dieid) \
            #                                                    .where(self._diemtab.c.Mask==mask)).all(),columns=['DieXY','dieid'])
            #        df2=df.reset_index() \
            #            .assign(loadid=loadid,Mask=mask).rename(columns={'index':'measid'}) \
            #            [['loadid','measid','Structure','DieXY','rawgroup',*meas_cols]].merge(diem,how='left',on='DieXY') \
            #            [['loadid','measid','Structure','dieid','rawgroup',*meas_cols]]
            #        self._upload_csv(df2,conn,self.int_schema,self._mgt(meas_group,'meas').name)


            df=df.merge(diem,how='left',on='DieXY').assign(**loadids)
            self._upload_csv(
                df[[*(loadids.keys()),'dieid',*CONFIG.higher_analyses[an]['analysis_columns']]],
                conn,self.int_schema,self._hat(an).name
            )

            conn.execute(delete(self._reatab)\
                .where(self._reatab.c.matid==matid)\
                .where(self._reatab.c.analysis==an))

            ## Invalidate the relevant analyses
            #insrt=""
            #for an in CONFIG.get_dependent_analyses(list(data_by_meas_group.keys())):
            #    insrt+= \
            #        str(pgsql_insert(self._reatab) \
            #            .values(matid=matid,analysis=an) \
            #            .compile(compile_kwargs={'literal_binds':True}))
            #conn.execute(text(insrt))



    def get_data_for_regen(self, meas_group, matname, on_no_data='raise'):
        meas_cols=list(CONFIG['measurement_groups'][meas_group]['meas_columns'])
        data=self.get_data(meas_group=meas_group,
                      scalar_columns=['loadid','measid','rawgroup','DieXY','DieCenterX [mm]','DieCenterY [mm]','Structure','Mask',*meas_cols],
                      include_sweeps=True, raw_only=True, unstack_headers=True,
                      **{CONFIG['database']['materials']['full_name']:[matname]})

        if not(len(data)):
            match on_no_data:
                case 'raise':
                    raise Exception(f"No data for re-extraction of {matname} with measurement group {meas_group}")
                case None:
                    return None


        headers=[]
        for c in data.columns:
            if c=='rawgroup': break
            headers.append(c)

        from datavac.io.measurement_table import MultiUniformMeasurementTable, UniformMeasurementTable

        meas_type=CONFIG.get_meas_type(meas_group)

        # This uses the fact that there is only one loadid for a given mg and matname
        return MultiUniformMeasurementTable([
            UniformMeasurementTable(dataframe=df.reset_index(drop=True),headers=headers,
                                    meas_length=None,meas_type=meas_type,meas_group=meas_group)
            for rg, df in data.groupby('rawgroup')])

    def get_data(self,meas_group,scalar_columns=None,include_sweeps=False,
                         unstack_headers=False,raw_only=False,**factors):
        if meas_group in CONFIG.higher_analyses:
            assert include_sweeps==False, f"For analysis (eg {meas_group}), include_sweeps must be False, not {include_sweeps}"
            assert unstack_headers==False
            assert raw_only==False
            return self.get_data_from_analysis(meas_group,scalar_columns=scalar_columns,**factors)
        elif meas_group in CONFIG.measurement_groups:
            return self.get_data_from_meas_group(meas_group,scalar_columns=scalar_columns,include_sweeps=include_sweeps,
                 unstack_headers=unstack_headers,raw_only=raw_only,**factors)
        else:
            raise Exception(f"What is '{meas_group}'?")


    def get_data_from_analysis(self,analysis,scalar_columns=None, **factors):
        anlytab=self._hat(analysis)
        involved_tables=([anlytab]+ \
                         [self._diemtab,self._loadtab,self._mattab])
        all_cols=[c for tab in involved_tables for c in tab.columns]
        def get_col(cname):
            try: return next(c for c in all_cols if c.name==cname)
            except StopIteration:
                raise Exception(f"Couldn't find column {cname} among {[c.name for c in all_cols]}")
        if scalar_columns:
            selcols=[get_col(sc) for sc in scalar_columns]
        else:
            selcols=list(set([get_col(sc.name) for sc in all_cols]))

        thejoin=functools.reduce((lambda x,y: x.join(y)),involved_tables)
        sel=select(*selcols).select_from(thejoin)
        sel=functools.reduce((lambda s, f: s.where(get_col(f).in_(factors[f]))), factors, sel)
        with self.engine.connect() as conn:
            data=pd.read_sql(sel,conn,dtype={c.name:self.sql_to_pd_types[c.type.__class__] for c in selcols
                                             if c.type.__class__ in self.sql_to_pd_types})
        return data

    def get_data_from_meas_group(self,meas_group,scalar_columns=None,include_sweeps=False,
                 unstack_headers=False,raw_only=False,**factors):
        meastab=self._mgt(meas_group,'meas')
        sweptab=self._mgt(meas_group,'sweep')
        extrtab=self._mgt(meas_group,'extr')
        layotab=self._mgt(meas_group,'layout')

        if include_sweeps not in [True,False]:
            if len(include_sweeps):
                factors['header']=include_sweeps
            else:
                include_sweeps=False
        involved_tables=(([extrtab] if not raw_only else [])+\
                            [meastab,self._diemtab,layotab,self._loadtab,self._mattab]\
                        +([sweptab] if include_sweeps else []))
        all_cols=[c for tab in involved_tables for c in tab.columns]
        def get_col(cname):
            try:
                return next(c for c in all_cols if c.name==cname)
            except StopIteration:
                raise Exception(f"Couldn't find column {cname} among {[c.name for c in all_cols]} from {[i.name for i in involved_tables]}")
        if scalar_columns:
            if (include_sweeps and unstack_headers):
                for sc in ['loadid','measid']:
                    if sc not in scalar_columns: scalar_columns=scalar_columns+[sc] # not inplace
            selcols=[get_col(sc) for sc in scalar_columns]
        else:
            selcols=list(set([get_col(sc.name) for sc in all_cols if sc.table is not sweptab]))
        selcols+=([sweptab.c.sweep,sweptab.c.header] if include_sweeps else [])

        ## TODO: Be more selective in thejoin
        thejoin=functools.reduce((lambda x,y: x.join(y)),involved_tables)
        sel=select(*selcols).select_from(thejoin)
        sel=functools.reduce((lambda s, f: s.where(get_col(f).in_(factors[f]))), factors, sel)

        with self.engine.connect() as conn:
            with time_it("Actual read_sql",threshold_time=.03):
                data=pd.read_sql(sel,conn,dtype={c.name:self.sql_to_pd_types[c.type.__class__] for c in selcols
                                                if c.type.__class__ in self.sql_to_pd_types})
        if 'sweep' in data:
            meas_type=CONFIG.get_meas_type(meas_group)
            for h in list(data['header'].unique()):
                assert meas_type.get_preferred_dtype(h)==np.float32,\
                    "Haven't dealt with sweeps that aren't float32"
            data['sweep']=data['sweep'].map(functools.partial(np.frombuffer, dtype=np.float32))
        if include_sweeps and unstack_headers:
            unstacking_indices= ['loadid','measid'] if unstack_headers is True else unstack_headers
            data=self._unstack_header_helper(data,unstacking_indices, drop_index=(not raw_only))
        return data

    @staticmethod
    def _unstack_header_helper(data,unstacking_indices, drop_index=True):
        sweep_part=data[[*unstacking_indices,'header','sweep']] \
            .pivot(index=unstacking_indices,columns='header',values='sweep')
        other_part=data.drop(columns=['header','sweep']) \
            .drop_duplicates(subset=unstacking_indices).set_index(unstacking_indices)
        return pd.merge(sweep_part,other_part,how='inner',
                        left_index=True,right_index=True,validate='1:1').reset_index(drop=drop_index)

    def get_factors(self,meas_group,factor_names,pre_filters={}):
        #import pdb; pdb.set_trace()
        assert meas_group in CONFIG['measurement_groups'], f"'{meas_group}' not in project measurement group listing"
        meastab=self._mgt(meas_group,'meas')
        sweptab=self._mgt(meas_group,'sweep')
        extrtab=self._mgt(meas_group,'extr')
        layotab=self._mgt(meas_group,'layout')

        involved_tables=[meastab,extrtab,layotab,self._mattab,self._diemtab]#+([sweptab] if 'header' in pre_filters else [])
        if any(t is None for t in involved_tables):
            raise Exception(f"WHAT {str(involved_tables)}")
        all_cols=[c for tab in involved_tables for c in tab.columns]
        def get_col(cname):
            try:
                return next(c for c in all_cols if c.name==cname)
            except StopIteration:
                raise Exception(f"Couldn't find column {cname} among {[c.name for c in all_cols]}")
        def apply_wheres(s):
            for pf,values in pre_filters.items():
                s=s.where(get_col(pf).in_(values))
            return s
        factor_cols=[get_col(f) for f in factor_names]
        # TODO: Be more selective in thejoin
        thejoin=extrtab.join(meastab).join(layotab).join(self._loadtab).join(self._mattab).join(self._diemtab)
        sel=union_all(*[apply_wheres(select(*factor_cols).select_from(thejoin)).distinct(f) for f in factor_cols])

        with self.engine.connect() as conn:
            records=conn.execute(sel).all()

        if not len(records): return {f:[] for f in factor_names}
        return {f:list(set(vals)) for f,vals in zip(factor_names,zip(*records))}


    def make_DSN(self,dsnpath):
        connection_info=dict([[s.strip() for s in x.split("=")] for x in
                              os.environ['DATAVACUUM_DBSTRING'].split(";")])
        if self._sslrootcert:
            escaped_rootfile=str(self._sslrootcert).replace('\\','\\\\')
        string=\
            f"""
            [ODBC]
            DRIVER=PostgreSQL Unicode(x64)
            UID={connection_info['Uid']}
            XaOpt=1
            FetchRefcursors=0
            OptionalErrors=0
            D6=-101
            {f'pqopt={{sslrootcert={escaped_rootfile}}}' if self._sslrootcert else ''}
            LowerCaseIdentifier=0
            UseServerSidePrepare=1
            ByteaAsLongVarBinary=1
            BI=0
            TrueIsMinus1=0
            UpdatableCursors=1
            LFConversion=1
            ExtraSysTablePrefixes=
            Parse=0
            BoolsAsChar=1
            UnknownsAsLongVarchar=0
            TextAsLongVarchar=1
            UseDeclareFetch=0
            CommLog=0
            Debug=0
            MaxLongVarcharSize=8190
            MaxVarcharSize=255
            UnknownSizes=0
            Fetch=100
            ShowSystemTables=0
            RowVersioning=0
            ShowOidColumn=0
            FakeOidIndex=0
            Protocol=7.4
            ReadOnly=0
            {f'SSLmode=verify-full' if connection_info['Server']!='localhost' else ''}
            PORT={connection_info['Port']}
            SERVER={connection_info['Server']}
            DATABASE={connection_info['Database']}
            """
        Path(dsnpath).parent.mkdir(parents=True,exist_ok=True)
        with open(dsnpath,'w') as f:
            f.write("\n".join([l.strip() for l in string.split("\n") if l.strip()!=""]))

    def make_JMPstart(self,jslpath=None):
        connection_info=dict([[s.strip() for s in x.split("=")] for x in
                              os.environ['DATAVACUUM_DBSTRING'].split(";")])
        if self._sslrootcert:
            escaped_rootfile=str(self._sslrootcert).replace('\\','\\\\')
        string=\
            fr"""
            New SQL Query(
                Connection(
                    "ODBC:DRIVER={{PostgreSQL Unicode(x64)}};
                    DATABASE={connection_info['Database']};
                    SERVER={connection_info['Server']};
                    PORT={connection_info['Port']};
                    UID={connection_info['Uid']};
                    PWD={connection_info['Password']};
                    {'SSLmode=verify-full;' if connection_info['Server']!='localhost' else ''}
                    ReadOnly=0;Protocol=7.4;FakeOidIndex=0;ShowOidColumn=0;RowVersioning=0;
                    ShowSystemTables=0;Fetch=100;UnknownSizes=0;MaxVarcharSize=255;MaxLongVarcharSize=8190;
                    Debug=0;CommLog=0;UseDeclareFetch=0;TextAsLongVarchar=1;UnknownsAsLongVarchar=0;BoolsAsChar=1;
                    Parse=0;LFConversion=1;UpdatableCursors=1;TrueIsMinus1=0;BI=0;ByteaAsLongVarBinary=1;
                    UseServerSidePrepare=1;LowerCaseIdentifier=0;
                    {f'pqopt={{sslrootcert={escaped_rootfile}}};' if self._sslrootcert else '' }
                    D6=-101;OptionalErrors=0;FetchRefcursors=0;XaOpt=1;"
                ),
                QueryName( "test_query" ),
                CustomSQL("Select * from information_schema.tables;"),
                PostQueryScript( "Close(Data Table(\!"test_query\!"), No Save);" )
                ) << Run;
                Print("Wait five seconds and check the connections.");
            """
        Path(jslpath).parent.mkdir(parents=True,exist_ok=True)
        with open(jslpath,'w') as f:
            f.write("\n".join([l.strip() for l in string.split("\n") if l.strip()!=""]))


##### NOT FUNCTIONAL
####class SQLiteDatabase(AlchemyDatabase):
####
####    def get_engine(self):
####        folder:Path=Path(os.environ['DATAVACUUM_CACHE_DIR'])/"db"
####        assert "//" not in str(folder) and "\\\\" not in str(folder), \
####            f"DATAVACUUM_CACHE_DIR points to a remote directory [{os.environ['DATAVACUUM_CACHE_DIR']}].  " \
####            "This would be miserably slow to use for SQLITE."
####        folder.mkdir(exist_ok=True)
####        self._sync_engine=create_engine(f"sqlite:///{folder}/SummaryData.db")

def cli_clear_database(*args):
    parser=argparse.ArgumentParser(description='Clears out the database [after confirming].')
    parser.add_argument('-y','--yes',action='store_true',help="Don't ask confirmation, just clear it.")
    parser.add_argument('-t','--table',action='append',help="Clear specific table(s), eg -t TAB1 -t TAB2")
    parser.add_argument('--keep_rex',action='store_true',help="Keep the materials and re-ex tables (ie leave enough info to heal)")
    namespace=parser.parse_args(args)

    db=get_database(skip_establish=True)
    try:
        only_tables=list(db._metadata.tables.values()) if namespace.table is None else [db._metadata.tables[t] for t in namespace.table]
    except KeyError as e:
        logger.critical(f"Couldn't find {str(e)}.")
        if '.' not in str(e):
           logger.critical("Did you forget to include the schema?")
        logger.critical(f"Options in include {list(db._metadata.tables.keys())}")
        return
    if namespace.keep_rex:
        only_tables=[t for t in only_tables
                     if t.name not in ["Materials",f"ReExtract"]]
    logger.info(f"Tables that will get cleared: {sorted([t.name for t in only_tables])}")
    if namespace.yes \
            or (prompt('Are you sure you want to clear the database? ').strip().lower()=='y'):
        logger.warning("Clearing database")
        db.clear_database(only_tables=only_tables)
        logger.warning("Done clearing database")

#def cli_update_diemaps(*args):
#    parser=argparse.ArgumentParser()
#    namespace=parser.parse_args(args)

def cli_update_layout_params(*args):
    parser=argparse.ArgumentParser(description='Updates layout params in database.')
    namespace=parser.parse_args(args)

    layout_params=LayoutParameters(force_regenerate=True)
    db=get_database(skip_establish=True)
    with db.engine.connect() as conn:
        for mg in layout_params._tables_by_meas:
            db.update_layout_parameters(layout_params,mg,conn)
        conn.commit()

def cli_dump_measurement(*args):
    parser=argparse.ArgumentParser(description='Dumps measurements')
    parser.add_argument('-g','--group',action='append',help='Measurement group(s) to drop, eg -g GROUP1 -g GROUP2')
    namespace=parser.parse_args(args)

    db=get_database(skip_establish=True)
    with db.engine.connect() as conn:
        for mg in (namespace.group if namespace.group else CONFIG.measurement_groups):
            db.dump_measurements(mg,conn)
        conn.commit()

def cli_dump_extraction(*args):
    parser=argparse.ArgumentParser(description='Dumps extractions')
    parser.add_argument('-g','--group',action='append',help='Measurement group(s) to drop, eg -g GROUP1 -g GROUP2')
    namespace=parser.parse_args(args)

    db=get_database(skip_establish=True)
    with db.engine.connect() as conn:
        for mg in (namespace.group if namespace.group else CONFIG.measurement_groups):
            db.dump_extractions(mg,conn)
        conn.commit()

def cli_force_database(*args):
    parser=argparse.ArgumentParser(description='Replaces any tables not currently in agreement with schema')
    namespace=parser.parse_args(args)
    db=get_database(on_mismatch='replace')

def cli_upload_data(*args):
    from datavac.io.meta_reader import ALL_MATERIAL_COLUMNS

    parser=argparse.ArgumentParser(description='Extracts and uploads data')
    for col in ALL_MATERIAL_COLUMNS:
        parser.add_argument(f'--{col.lower()}',action='append',
            help=f"Restrict to specified {col}(s): "\
                 f"eg --{col.lower()} {col.upper()}1 --{col.lower()} {col.upper()}2")
    parser.add_argument('-f','--folder',action='append',
                        help=f"Restrict to specified folder(s): " \
                             f"eg -f FOLDER1 -f FOLDER2.  Default is all of {os.environ['DATAVACUUM_READ_DIR']}")
    parser.add_argument('-g','--group',action='append',help='Restrict to measurement group(s), eg -g GROUP1 -g GROUP2')
    parser.add_argument('-k','--keep_other_groups',action='store_true',
                        help=f"Keep measurement data related measurement groups that are not present in this upload.  "
                             f"(Default is to drop all data related to a given material when uploading afresh).")

    namespace=parser.parse_args(args)

    folders=namespace.folder
    only_material={col: getattr(namespace,col.lower()) for col in ALL_MATERIAL_COLUMNS}
    only_material={k:v for k,v in only_material.items() if v is not None}

    db=get_database()
    read_and_upload_data(db, folders, only_material=only_material, only_meas_groups=namespace.group,
        clear_all_from_material=(not namespace.keep_other_groups),user_called=True)

def read_and_upload_data(db,folders=None,only_material={},only_meas_groups=None,clear_all_from_material=True, user_called=True):
    from datavac.io.meta_reader import read_and_analyze_folders

    if folders is None:
        if ((connected:=CONFIG.meta_reader.get('connect_toplevel_folder_to',None)) and (connected in only_material)):
            folders=only_material[connected]
        else:
            if not (prompt('No folder or lot restriction, continue to read EVERYTHING? [y/n] ').strip().lower()=='y'):
                return

    logger.info(f"Will read folder(s) {' and '.join(folders)}")
    matname_to_data,matname_to_inf=read_and_analyze_folders(folders,
                only_material_info=only_material, only_meas_groups=only_meas_groups)
    for matname in matname_to_data:
        logger.info(f"Uploading {matname}")
        db.push_data(matname_to_inf[matname],matname_to_data[matname],
                     clear_all_from_material=clear_all_from_material, user_called=user_called)
def heal(db: PostgreSQLDatabase,force_all_meas_groups=False):
    """ Goes in order of most recent material first, and within that, reloads first than re-extractions."""
    from datavac.io.meta_reader import perform_extraction
    fullname=CONFIG['database']['materials']['full_name']
    matcolnames=[*CONFIG['database']['materials']['info_columns']]
    with db.engine.connect() as conn:
        res=conn.execute(select(db._rextab.c.matid,
                                *[db._mattab.c[n] for n in [fullname]+matcolnames],)\
                         .select_from(db._rextab.join(db._mattab))\
                         .order_by(db._mattab.c.date_user_changed.desc())).all()
        if not len(res):
            logger.info("Nothing needs re-loading or re-extracting!")
        else:
            for matid,matname,*other_matinfo in res:
                # Reloads
                logger.info(f"Looking at {matname}")
                res=conn.execute(select(db._rextab.c.MeasGroup) \
                                 .where(db._rextab.c.matid==matid)\
                                 .where(db._rextab.c.full_reload==True)).all()
                if not len(res):
                    logger.info("Nothing to reload")
                else:
                    meas_groups=None if force_all_meas_groups else [r[0] for r in res]
                    all_meas_groups=ensure_meas_group_sufficiency(meas_groups,on_error='ignore')
                    read_and_upload_data(db,
                         folders=None,only_material=dict(**{fullname:matname},**dict(zip(matcolnames,[[om] for om in other_matinfo]))),
                         only_meas_groups=all_meas_groups,
                         clear_all_from_material=False,user_called=False)

                # Re-extracts
                res=conn.execute(select(db._rextab.c.MeasGroup) \
                                 .where(db._rextab.c.matid==matid) \
                                 .where(db._rextab.c.full_reload==False)).all()
                if not len(res):
                    logger.info("Nothing to re-extract")
                else:
                    meas_groups=[r[0] for r in res]
                    all_meas_groups=ensure_meas_group_sufficiency(meas_groups,on_error='ignore')
                    logger.info(f"Pulling sweeps for {all_meas_groups} to re-extract {meas_groups}")
                    mumts={mg:db.get_data_for_regen(mg,matname=matname,on_no_data=None) for mg in all_meas_groups}
                    mumts={k:v for k,v in mumts.items() if v is not None}
                    logger.info(f"Re-extracting {meas_groups}")
                    perform_extraction({matname:mumts})
                    logger.info(f"Pushing new extraction for {meas_groups}")
                    db.push_data({fullname:matname},{k:v for k,v in mumts.items() if k in meas_groups},
                                 clear_all_from_material=False,
                                 user_called=False, re_extraction=True)

        res=conn.execute(select(db._reatab.c.matid,
                                *[db._mattab.c[n] for n in [fullname]+matcolnames],) \
                         .select_from(db._reatab.join(db._mattab)) \
                         .order_by(db._mattab.c.date_user_changed.desc())).all()
        if not len(res):
            logger.info("Nothing needs re-analyzing!")
        else:
            for matid,matname,*other_matinfo in res:
                # Re-analyses
                res=conn.execute(select(db._reatab.c.analysis) \
                                 .where(db._reatab.c.matid==matid)).all()
                if not len(res):
                    logger.info("Nothing to re-analyze")
                else:
                    analyses=[r[0] for r in res]
                    req_meas_groups=CONFIG.get_dependency_meas_groups_for_analyses(analyses,required_only=True)
                    all_meas_groups=CONFIG.get_dependency_meas_groups_for_analyses(analyses,required_only=False)
                    logger.info(f"Pulling measured and extracted data for {list(dict(**all_meas_groups).keys())}")
                    mumts={}
                    from datavac.io.measurement_table import MultiUniformMeasurementTable, UniformMeasurementTable
                    precol_loadids={}
                    for mg in all_meas_groups:
                        mg_info = CONFIG['measurement_groups'][mg]
                        data=db.get_data(meas_group=mg, include_sweeps=False,
                                         scalar_columns=[*mg_info['meas_columns'],*mg_info['analysis_columns'],
                                                         'loadid','DieXY','Structure',
                                                         CONFIG['database']['materials']['full_name'],
                                                         *CONFIG['database']['materials']['info_columns']],
                                         **{CONFIG['database']['materials']['full_name']:[matname]})
                        if mg in req_meas_groups:
                            assert len(data), f"Missing required data for {mg}"
                        if len(data):
                            mumts[mg]=UniformMeasurementTable(dataframe=data,headers=[],
                                                              meas_length=None,meas_type=None,meas_group=mg)
                        assert len(list(data['loadid'].unique()))==1
                        precol_loadids[mg]=data['loadid'].iloc[0]
                    logger.info(f"Re-analyzing {analyses}")
                    db.perform_analyses(conn,analyses=analyses,
                                        precollected_data_by_meas_group=mumts,
                                        precollected_loadids=precol_loadids,
                                        precollected_matid=matid)
                    conn.commit()

        logger.info(f"Done healing.")

def cli_heal(*args):
    parser=argparse.ArgumentParser(description='Tries to re-extract or re-load dumped info')
    parser.add_argument('-a','--all_measgroups',action='store_true',help="Force re-upload of all meas groups for each healed lot")
    namespace=parser.parse_args(args)
    db=get_database()
    heal(db,force_all_meas_groups=namespace.all_measgroups)

def cli_clear_reextract_list(*args):
    parser=argparse.ArgumentParser(description='Clears the list of items which will be re-extracted upon healing')
    parser.add_argument('-g','--group',action='append',help='Measurement group(s) to clear from list, eg -g GROUP1 -g GROUP2')
    namespace=parser.parse_args(args)

    self=get_database(skip_establish=True)

    dstat=delete(self._rextab)
    if namespace.group is not None:
        dstat=dstat.where(self._rextab.c.MeasGroup.in_(namespace.group))
    with self.engine.begin() as conn:
        conn.execute(dstat)

def cli_print_database():
    connection_info=dict([[s.strip() for s in x.split("=")] for x in
                          os.environ['DATAVACUUM_DBSTRING'].split(";")])
    print(connection_info['Server'])

def entry_point_make_jmpstart():
    try:
        jslfile=sys.argv[1]
    except IndexError:
        raise Exception("Supply a path for the JSL file you want to produce," \
                        " eg 'datavac_make_jmpstart JMP_DIR/this.jsl'.")
    get_database().make_JMPstart(jslfile)

def cli_update_mask_info(*args):
    parser=argparse.ArgumentParser(description='Updates the mask information in the database')
    namespace=parser.parse_args(args)
    self=get_database(skip_establish=True)
    with self.engine.connect() as conn:
        self.update_mask_info(conn)
        conn.commit()

class DDFDatabase(Database):
    def __init__(self,ddf={}):
        self._ddf=ddf
    def get_data(self,meas_group,scalar_columns=None,include_sweeps=False,unstack_headers=False,raw_only=False,**factors):
        assert unstack_headers
        assert not raw_only

        df=self._ddf[meas_group]
        avail_header_cols=[k for k,v in df.dtypes.items() if str(v)=='object']
        avail_scalar_cols=[k for k in df.columns if k not in avail_header_cols]

        df=df[functools.reduce(np.logical_and,
                     [df[fname].isin(fvals) for fname,fvals in factors.items()],
                     pd.Series([True]*len(df)))]

        cols=[*(avail_header_cols if include_sweeps is True else include_sweeps if include_sweeps else []),
              *(scalar_columns if scalar_columns else avail_scalar_cols)]
        return df[cols].reset_index()
    def get_factors(self,meas_group,factor_names,pre_filters={}):
        df=self._ddf[meas_group]
        df=df[functools.reduce(np.logical_and,
                     [df[fname].isin(fvals) for fname,fvals in pre_filters.items()],
                     pd.Series([True]*len(df)))]
        return {fn:list(df[fn].unique()) for fn in factor_names}