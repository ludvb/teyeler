#!/bin/env python3

"""Image tiler for the SpatialEye ST App"""

import argparse as ap

from enum import Enum

from functools import partial

import logging

from math import ceil, log2

import os

import queue

import threading

import numpy as np

from scipy.misc import imread, imsave
from scipy.ndimage.interpolation import zoom as libzoom


class Corner(Enum):
    """
    Enum of the ''corner'' of the tilemap (i.e., where the (0, 0) coordinate
    is).
    """
    SouthWest = 0  # TMS default
    NorthWest = 1
    SouthEast = 2
    NorthEast = 3

class Order(Enum):
    """
    Enum of the coordinate order of the tilemap. If stored in `Order.ColMajor`
    order, coordinates are given as `(c, r)`, where `c` is the column and `r` is
    the row of the tilemap. Conversely, if set to `Order.RowMajor`, coordinates
    will be given as `(r, c)`.
    """
    ColMajor = 0
    RowMajor = 1


DEF_TILE_SIZE = [256, 256]
DEF_ORDER = Order.ColMajor
DEF_CORNER = Corner.SouthWest

DEF_THREADS = 8


class FormattedLogRecord(logging.LogRecord):
    """LogRecord that accepts used-defined format codes"""
    # pylint: disable=too-few-public-methods
    def __init__(self, fmt_codes, *args):
        for key, val in fmt_codes.items():
            setattr(self, key, val)
        super().__init__(*args)

class FormattedLogRecordFactory(object):
    """Factory for `FormattedLogRecord`"""
    # pylint: disable=too-few-public-methods
    color_codes = {
        'green': '\033[92m',
        'blue': '\033[94m',
        'red': '\033[91m',
        'purple': '\033[95m',
        'bold': '\033[1m',
        'end': '\033[0m',
    }

    def __init__(self, level_formats):
        self.level_formats = level_formats

    def __call__(self, name, level, fname, lno, msg, args, exc_info,
                 _func=None, _sinfo=None, **kwargs):
        # pylint: disable=too-many-arguments
        return FormattedLogRecord(
            {
                'start': self.level_formats[logging.getLevelName(level)],
                'end': self.color_codes['end'],
            },
            name, level, fname, lno, msg, args, exc_info,
        )


# Set up the logger
logging.basicConfig(
    format=(
        '{:s}[{:s}'.format(
            FormattedLogRecordFactory.color_codes['bold'],
            FormattedLogRecordFactory.color_codes['end'],
        ) +
        '%(asctime)s' +
        '{:s} : {:s}'.format(
            FormattedLogRecordFactory.color_codes['bold'],
            FormattedLogRecordFactory.color_codes['end'],
        ) +
        '%(threadName)s' +
        '{:s} : {:s}'.format(
            FormattedLogRecordFactory.color_codes['bold'],
            FormattedLogRecordFactory.color_codes['end'],
        ) +
        '%(filename)s:%(lineno)d' +
        '{:s}]{:s}'.format(
            FormattedLogRecordFactory.color_codes['bold'],
            FormattedLogRecordFactory.color_codes['end'],
        ) +
        '  %(start)s{:s}%(levelname)s{:s}%(start)s: %(message)s%(end)s'.format(
            FormattedLogRecordFactory.color_codes['bold'],
            FormattedLogRecordFactory.color_codes['end'],
        )
    )
)
logging.setLogRecordFactory(FormattedLogRecordFactory(
    level_formats={
        'WARNING': FormattedLogRecordFactory.color_codes['bold'] + \
            FormattedLogRecordFactory.color_codes['red'],
        'INFO': FormattedLogRecordFactory.color_codes['green'],
        'DEBUG': FormattedLogRecordFactory.color_codes['purple'],
    }
))
LOG = logging.getLogger()


class Threader(object):
    """
    Helper class for multithreading tasks.

    Parameters
    ----------
    nthreads : int
        The number of threads to use.
    """
    def __init__(self, nthreads):
        self.queue = queue.Queue()
        self.nthreads = nthreads
        self.threads = []

    def add(self, item):
        """
        Add task to queue.

        Parameters
        ----------
        item : callable
            The job to be executed.

        Returns
        -------
        self : Threader
            The current instance.
        """
        self.queue.put(item)
        return self

    def await(self):
        """
        Wait for all queued tasks to complete.

        Returns
        -------
        self : Threader
            The current instance.
        """
        self.queue.join()
        return self

    def start(self):
        """
        Start task consumption.

        Returns
        -------
        self : Threader
            The current instance.
        """
        self.threads.clear()
        for _ in range(self.nthreads):
            thread = threading.Thread(target=self._worker)
            self.threads.append(thread)
            thread.start()
        return self

    def stop(self):
        """
        Wait for all queued tasks to complete and stop all threads.

        Returns
        -------
        self : Threader
            The current instance.
        """
        self.await()
        for _ in self.threads:
            self.queue.put(None)
        for thread in self.threads:
            thread.join()
        return self

    def _worker(self):
        """Worker event loop"""
        while True:
            job = self.queue.get()
            if job is None:
                break
            job()
            self.queue.task_done()


def zoom(img):
    """
    Zoom image by a factor of 2.

    Parameters
    ----------
    img : numpy.ndarray
        The input image.

    Returns
    -------
    zoomed : numpy.ndarray
        The zoomed image.
    """
    LOG.info('Zooming image from {:d}x{:d} to {:d}x{:d}.'.format(
        *img.shape[:2],
        *[s // 2 for s in img.shape[:2]],
    ))
    return libzoom(img, (0.5, 0.5, 1), order=0)

def pad(corner, shape, img, fill=None, order='C'):
    """
    Pad image to shape by expanding the image array from a given corner.

    The operation is done in-place.

    Parameters
    ----------
    corner : Corner
        The corner to expand from.
    shape : array_like
        Target shape. Must be a 2-tuple in which each coordinate is greater than
        or equal to `img[:2]`.
    img : numpy.ndarray
        The input image.
    fill : numpy.ndarray, optional
        The value to fill the padded pixels with.
    order : str, optional
        Array order. Default = 'C'.
    """
    old_shape = img.shape[:2]
    px_shape = img.shape[2:]
    px_len = np.prod(px_shape)
    new_shape = tuple(list(shape) + list(px_shape))

    if fill is None:
        fill = np.zeros(px_shape)

    marv, marh = [r - s for (r, s) in zip(shape, old_shape)]
    if marv < 0 or marh < 0:
        raise ValueError('Target shape is smaller than the shape of the image.')

    top, bottom = 0, marv
    if corner == Corner.SouthWest or corner == Corner.SouthEast:
        top, bottom = bottom, top
    bottom = new_shape[0] - bottom - 1

    left, right = 0, marh
    if corner == Corner.NorthEast or corner == Corner.SouthEast:
        left, right = right, left
    right = new_shape[1] - right - 1

    LOG.info('Padding image from {:d}x{:d} to {:d}x{:d}'.format(
        *old_shape[:2],
        *new_shape[:2],
    ))

    img.resize(new_shape, refcheck=False)
    flat = np.ravel(img, order=order)
    for row in reversed(range(old_shape[0])):
        old_idx = row * old_shape[1]
        new_idx = (row + top) * shape[1] + left
        flat[new_idx * px_len : (new_idx + old_shape[1]) * px_len] = \
            flat[old_idx * px_len : (old_idx + old_shape[1]) * px_len]

    img[:top, :] = img[bottom:, :] = img[:, :left] = img[:, right:] = fill

def tilecoordinates(order, corner, shape, coordinates):
    """
    Translate tile coordinates given in ''image coordinates'' to another basis.
    If `order == Order.RowMajor` and `corner == Corner.NorthWest`, this is the
    identity function.

    Parameters
    ----------
    order : Order
        See `Order`.
    corner : Corner
        See `Corner`.
    shape : array_like
        The shape of the tilemap.
    coordinates : array_like
        The coordinates of the tile in ''image coordinates'', i.e., `(row,
        column)`, where `(0, 0)` represents the north west corner of the image.

    Returns
    -------
    new_coordinates : tuple
        A 2-tuple `(maj, min)` where `maj` is the major index and `min` is the
        minor index of the tile.
    """
    rows, cols = shape
    row, col = coordinates
    if corner == Corner.SouthEast or corner == Corner.NorthEast:
        col = cols - col - 1
    if corner == Corner.SouthWest or corner == Corner.SouthEast:
        row = rows - row - 1
    return (col, row) if order == Order.ColMajor else (row, col)

def gettiles(tile_shape, img, corner=Corner.SouthWest, order=Order.ColMajor):
    """
    Tile image.

    *Note about side-effects*: If the shape of the image is not divisible by the
    tile shape, the image will be padded. Padding is done in-place, so the `img`
    object will be modified.

    Parameters
    ----------
    tile_shape : array_like
        The shape of the tiles.
    img : numpy.ndarray
        The input image.
    corner : Corner, optional
        See `Corner`. Defaults to `Corner.SouthWest`.
    order : Order, optional
        See `Order`. Defaults to `Order.ColMajor`.

    Yields
    ------
    tile : numpy.ndarray
        The output tile.
    coordinate : tuple
        A 2-tuple `(maj, min)` where `maj` is the major index and `min` is the
        minor index of the tile.
    """
    if not all(map(lambda x: (x[0] % x[1]) == 0, zip(img.shape, tile_shape))):
        LOG.warning('Image shape is not divisible by tile shape. '
                    'Image will be padded.')
        pad(
            corner,
            [
                s * n for (s, n) in zip(
                    tile_shape,
                    [ceil(s / t) for (s, t) in zip(img.shape, tile_shape)]
                )
            ],
            img,
        )
    rows, cols = map(lambda x: x[0] // x[1], zip(img.shape, tile_shape))
    tilecoordinates_ = partial(tilecoordinates, order, corner, (rows, cols))
    for (row, col) in ((r, c) for r in range(rows) for c in range(cols)):
        yield (
            img[
                slice(tile_shape[0] * row, tile_shape[0] * (row + 1)),
                slice(tile_shape[1] * col, tile_shape[1] * (col + 1))
            ],
            tilecoordinates_((row, col)),
        )

def tile(tile_shape, img, nlevels=None):
    """
    Tile image over different zoom levels.

    Parameters
    ----------
    tile_shape : array_like
        The shape of the tiles.
    img : numpy.ndarray
        The input image.
    nlevels : {None, int}, optional
        The maximum zoom level. If set to n, 2^n px in the input image will
        correspond to 1 px in the tiled image on the lowest (0:th) zoom level.
        If set to None, will be set to the smallest value needed to fit the
        entire image on a single tile at the lowest zoom level. Default: None.

    Yields
    ------
    tile : numpy.ndarray
        The output tile.
    major : int
        The major index of the tile.
    minor : int
        The minor index of the tile.
    level : int
        The zoom level of the `tiles`
    """
    if nlevels is None:
        nlevels = 1 + max([
            ceil(log2(s / t))
            for (s, t) in zip(img.shape, tile_shape)
        ])
    for level in reversed(range(nlevels)):
        for curtile, (major, minor) in gettiles(tile_shape, img):
            yield curtile, major, minor, level
        if level > 0:
            img = zoom(img)

def save(output_dir, major, minor, level, image_tile):
    """
    Save tile according to the format required by the SpatialEye ST app.

    Parameters
    ----------
    output_dir : str
        Root output directory.
    major : int
        The major order coordinate of the tile. For example, if the tiles are
        given in `Order.RowMajor` order, this is the row of the tile.
    minor : int
        The minor order coordinate of the tile.
    level : int
        The zoom level of the tile.
    image_tile : numpy.ndarray
        The tile to save.
    """
    dirname = os.path.join(
        output_dir,
        str(level),
        str(major),
    )
    basename = '{:d}.png'.format(minor)
    filename = os.path.join(dirname, basename)
    LOG.debug('Writing tile lxrxc={:d}x{:d}x{:d} to {:s}.'.format(
        level,
        major,
        minor,
        filename,
    ))
    os.makedirs(dirname, exist_ok=True)
    imsave(filename, image_tile)

def climain():
    """Entry point for cli"""
    opt = ap.ArgumentParser()
    opt.add_argument('-i', '--input', required=True, help='path to input image')
    opt.add_argument('-o', '--output', required=True, help='output directory')
    opt.add_argument('--shape', type=list, default=DEF_TILE_SIZE,
                     help='tile dimensions. '
                          'default = {:d} {:d}.'.format(*DEF_TILE_SIZE))
    opt.add_argument('--levels', type=int, default=None,
                     help='max zoom level. if set to n, 2^n px in the input '
                          'image will correspond to 1 px in the tiled image on '
                          'the lowest (0:th) zoom level. by default, this '
                          'will be set to the smallest value needed to fit the '
                          'entire image on a single tile at the lowest zoom '
                          'level.')
    opt.add_argument('-v', '--verbose', action='store_true',
                     help='verbose output.')
    opt.add_argument('--silent', action='store_true',
                     help='suppress all log messages that are not warnings or '
                          'errors')
    opt.add_argument('--threads', type=int, default=DEF_THREADS,
                     help='number of threads to use for file I/O. '
                          'default = {:d}'.format(DEF_THREADS))
    opt = opt.parse_args()

    if opt.silent:
        LOG.setLevel(logging.WARN)
    elif opt.verbose:
        LOG.setLevel(logging.DEBUG)
    else:
        LOG.setLevel(logging.INFO)

    threader = Threader(opt.threads).start()
    for curtile, row, col, lvl in tile(
            opt.shape,
            imread(opt.input, mode='RGBA'),
            opt.levels
    ):
        threader.add(partial(save, opt.output, row, col, lvl, curtile))
    LOG.info('Waiting for threads to finish writing.')
    threader.stop()
    LOG.info('Done.')

if __name__ == "__main__":
    climain()
