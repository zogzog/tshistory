import os
import math
import zlib
import struct
import logging
import threading
import tempfile
import shutil
from contextlib import contextmanager
from pathlib import Path

import numpy as np
import pandas as pd
from pandas.api.types import is_datetime64tz_dtype
from sqlalchemy.engine import url
from sqlalchemy.engine.base import Engine
from inireader import reader


@contextmanager
def tempdir(suffix='', prefix='tmp'):
    tmp = tempfile.mkdtemp(suffix=suffix, prefix=prefix)
    try:
        yield Path(tmp)
    finally:
        shutil.rmtree(tmp)


def get_cfg_path():
    if 'TSHISTORYCFGPATH' is os.environ:
        cfgpath = Path(os.environ['TSHISTORYCFGPATH'])
        if cfgpath.exists():
            return cfgpath
    cfgpath = Path('tshistory.cfg')
    if cfgpath.exists():
        return cfgpath
    cfgpath = Path('~/tshistory.cfg').expanduser()
    if cfgpath.exists():
        return cfgpath


def find_dburi(something: str) -> str:
    try:
        url.make_url(something)
    except Exception:
        pass
    else:
        return something

    # lookup in the env, then in cwd, then in the home
    cfgpath = get_cfg_path()
    if not cfgpath:
        raise Exception('could not use nor look up the db uri')

    try:
        cfg = reader(cfgpath)
        return cfg['dburi'][something]
    except Exception as exc:
        raise Exception(
            f'could not find the `{something}` entry in the '
            f'[dburi] section of the `{cfgpath.resolve()}` '
            f'conf file (cause: {exc.__class__.__name__} -> {exc})'
        )


def tzaware_serie(ts):
    return is_datetime64tz_dtype(ts.index)


def pruned_history(hist):
    if not hist:
        return hist
    idates = list(hist.keys())
    idate = idates[0]
    current = hist[idate]
    pruned = {
        idate: current
    }
    for idate in idates[1:]:
        newts = hist[idate]
        if not current.equals(newts):
            pruned[idate] = newts
            current = newts
    return pruned


def start_end(ts, notz=True):
    ts = ts.dropna()
    if not len(ts):
        return None, None
    start = ts.index.min()
    end = ts.index.max()
    if start.tzinfo is not None and notz:
        assert end.tzinfo is not None
        start = start.tz_convert('UTC').replace(tzinfo=None)
        end = end.tz_convert('UTC').replace(tzinfo=None)
    return start, end


def closed_overlaps(fromdate, todate):
    fromdate = "'-infinity'" if fromdate is None else '%(fromdate)s'
    todate = "'infinity'" if todate is None else '%(todate)s'
    return f'({fromdate}, {todate}) overlaps (tsstart, tsend + interval \'1 microsecond\')'


def inject_in_index(serie, revdate):
    mindex = [(revdate, valuestamp) for valuestamp in serie.index]
    serie.index = pd.MultiIndex.from_tuples(mindex, names=[
        'insertion_date', 'value_date']
    )


def numpy_serialize(series, isstr=False):
    # use `view` as a workarround for "cannot include dtype 'M' in a buffer"
    index = np.ascontiguousarray(
        series.index.values
    ).view(np.uint8).data.tobytes()

    if isstr:
        # string separatd by 0 and nones/nans represented as 3 (ETX)
        END, ETX = b'\0'.decode(), b'\3'.decode()
        # first, safety belt
        for s in series.values:
            if not pd.isnull(s):
                assert END not in s and ETX not in s
        values = b'\0'.join(
            b'\3' if pd.isnull(v) else v.encode('utf-8')
            for v in series.values
        )
    else:
        values = series.values.data.tobytes()

    return index, values


def binary_pack(index, values):
    """get series index and values as bytes and return a compressed bytes
    stream

    """
    index_size = struct.pack('!L', len(index))
    return zlib.compress(index_size + index + values)


def num2float(pdobj):
    # get a Series or a Dataframe column
    if str(pdobj.dtype).startswith('int'):
        return pdobj.astype('float64')
    return pdobj


def tojson(ts, precision=1e-14):
    return ts.to_json(date_format='iso',
                      double_precision=-int(math.log10(precision)))


def fromjson(jsonb, tsname, tzaware=False):
    series = _fromjson(jsonb, tsname).fillna(value=np.nan)
    if tzaware:
        series.index = series.index.tz_localize('utc')
    return series


def _fromjson(jsonb, tsname):
    if jsonb == '{}':
        return pd.Series(name=tsname)

    result = pd.read_json(jsonb, typ='series', dtype=False)
    result.name = tsname
    result = num2float(result)
    return result


class SeriesServices(object):
    _precision = 1e-14

    # diff handling

    def patch(self, base, diff):
        assert base is not None
        assert diff is not None
        basei = base.index
        diffi = diff.index
        newindex = basei.union(diffi)
        patched = pd.Series([0] * len(newindex), index=newindex)
        patched[basei] = base
        patched[diffi] = diff
        patched.name = base.name
        return patched

    def diff(self, base, other):
        if base is None:
            return other
        base = base[~base.isnull()]
        if not len(base):
            return other

        mask_overlap = other.index.isin(base.index)
        base_overlap = base[other.index[mask_overlap]]
        other_overlap = other[mask_overlap]

        if base.dtype == 'float64':
            mask_equal = np.isclose(base_overlap, other_overlap,
                                    rtol=0, atol=self._precision)
        else:
            mask_equal = base_overlap == other_overlap

        mask_na_equal = base_overlap.isnull() & other_overlap.isnull()
        mask_equal = mask_equal | mask_na_equal

        diff_overlap = other[mask_overlap][~mask_equal]
        diff_new = other[~mask_overlap]
        diff_new = diff_new[~diff_new.isnull()]
        return pd.concat([diff_overlap, diff_new])


def delete_series(engine, series, namespace='tsh'):
    from tshistory.tsio import timeseries
    tsh = timeseries(namespace=namespace)

    for name in series:
        with engine.begin() as cn:
            if not tsh.exists(cn, name):
                print('skipping unknown', name)
                continue
            print('delete', name)
            tsh.delete(cn, name)


def threadpool(maxthreads):
    L = logging.getLogger('parallel')

    def run(func, argslist):
        count = 0
        threads = []
        L.debug('// run %s %s', func.__name__, len(argslist))

        # initial threads
        for count, args in enumerate(argslist, start=1):
            th = threading.Thread(target=func, args=args)
            threads.append(th)
            L.debug('// start thread %s', th.name)
            th.daemon = True
            th.start()
            if count == maxthreads:
                break

        while threads:
            for th in threads[:]:
                th.join(1. / maxthreads)
                if not th.is_alive():
                    threads.remove(th)
                    L.debug('// thread %s exited, %s remaining', th.name, len(threads))
                    if count < len(argslist):
                        newth = threading.Thread(target=func, args=argslist[count])
                        threads.append(newth)
                        L.debug('// thread %s started', newth.name)
                        newth.daemon = True
                        newth.start()
                        count += 1

    return run


def _set_cache(txobj):
    txobj.cache = {
        'series_tablename': {},
        'metadata': {}
    }
    return txobj


def tx(func):
    " a decorator to check that the first method argument is a transaction "
    def check_tx_and_call(self, cn, *a, **kw):
        # safety belt to make sure important api points are tx-safe
        if not isinstance(cn, Engine):
            if not cn.in_transaction():
                raise TypeError('You must use a transaction object')
        else:
            with cn.begin() as txcn:
                return func(self, _set_cache(txcn), *a, **kw)

        return func(self, _set_cache(cn), *a, **kw)
    check_tx_and_call.__name__ = func.__name__
    return check_tx_and_call


class unilist(list):
    " a list which refuses duplicates "

    def append(self, element):
        assert element not in self
        super().append(element)


def bisect_search(values, value):
    """return an index j such that ``value`` is between values[j]
    and values[j+1].
    values must be monotonic increasing.

    j=-1 or j=len(values) is returned to indicate that ``value`` is
    out of range below and above respectively.

    thanks to https://stackoverflow.com/questions/2566412/find-nearest-value-in-numpy-array
    """
    n = len(values)
    first, last = values[0], values[-1]
    if value < first:
        return -1
    elif value > last:
        return n
    elif value == first:
        return 0
    elif value == last:
        return n - 1

    jl = 0
    ju = n - 1
    while ju - jl > 1:
        jm = (ju+jl) >> 1
        if value >= values[jm]:
            jl = jm
        else:
            ju = jm

    return jl

