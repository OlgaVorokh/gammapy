# Licensed under a 3-clause BSD style license - see LICENSE.rst
from __future__ import absolute_import, division, print_function, unicode_literals
import logging
from subprocess import call
from tempfile import NamedTemporaryFile
from copy import deepcopy
from collections import OrderedDict, namedtuple
import numpy as np
from numpy.lib.arraypad import _validate_lengths
from astropy.io import fits
from astropy.coordinates import SkyCoord, Angle
from astropy.coordinates.angle_utilities import angular_separation
from astropy.units import Quantity, Unit
from astropy.nddata.utils import Cutout2D
from regions import PixCoord, PixelRegion, SkyRegion
from astropy.wcs import WCS, WcsError
from astropy.wcs.utils import (pixel_to_skycoord, skycoord_to_pixel,
                               proj_plane_pixel_scales)
from ..extern.bunch import Bunch
from ..utils.scripts import make_path
from ..utils.wcs import get_resampled_wcs
from ..image.utils import make_header, _bin_events_in_cube
from ..data import EventList

__all__ = ['SkyImage', 'SkyImageCollection']

log = logging.getLogger(__name__)

_DEFAULT_WCS_ORIGIN = 0
_DEFAULT_WCS_MODE = 'all'


class SkyImage(object):
    """
    Sky image.

    Parameters
    ----------
    name : str
        Name of the image.
    data : `~numpy.ndarray`
        Data array.
    wcs : `~astropy.wcs.WCS`
        WCS transformation object.
    unit : str
        String specifying the data units.
    meta : `~collections.OrderedDict`
        Dictionary to store meta data.
    """
    _AxisIndex = namedtuple('AxisIndex', ['x', 'y'])
    _ax_idx = _AxisIndex(x=1, y=0)

    def __init__(self, name=None, data=None, wcs=None, unit=None, meta=None):
        # TODO: validate inputs
        self.name = name
        self.data = data
        self.wcs = wcs

        if meta is None:
            self.meta = OrderedDict()
        else:
            self.meta = OrderedDict(meta)

        self.unit = unit

    @property
    def center_pix(self):
        """Center pixel coordinate of the image (`~regions.PixCoord`)."""
        x = 0.5 * (self.data.shape[self._ax_idx.x] - 1)
        y = 0.5 * (self.data.shape[self._ax_idx.y] - 1)
        return PixCoord(x=x, y=y)

    @property
    def center(self):
        """Center sky coordinate of the image (`~astropy.coordinates.SkyCoord`)."""
        center = self.center_pix
        return SkyCoord.from_pixel(
            xp=center.x,
            yp=center.y,
            wcs=self.wcs,
            origin=_DEFAULT_WCS_ORIGIN,
            mode=_DEFAULT_WCS_MODE,
        )

    @classmethod
    def read(cls, filename, **kwargs):
        """Read image from FITS file.

        Parameters
        ----------
        filename : str
            FITS file name
        **kwargs : dict
            Keyword arguments passed `~astropy.io.fits.getdata`.
        """
        filename = str(make_path(filename))
        data = fits.getdata(filename, **kwargs)
        header = fits.getheader(filename, **kwargs)
        image_hdu = fits.ImageHDU(data, header)
        return cls.from_image_hdu(image_hdu)

    @classmethod
    def from_image_hdu(cls, image_hdu):
        """
        Create image from ImageHDU.

        Parameters
        ----------
        image_hdu : `astropy.io.fits.ImageHDU`
            Source image HDU.

        Examples
        --------
        >>> from astropy.io import fits
        >>> from gammapy.image import SkyImage
        >>> hdu_list = fits.open('data.fits')
        >>> image = SkyImage.from_image_hdu(hdu_list['myimage'])
        """
        data = image_hdu.data
        header = image_hdu.header
        wcs = WCS(image_hdu.header)

        name = header.get('HDUNAME')
        if name is None:
            name = header.get('EXTNAME')
        try:
            # Validate unit string
            unit = Unit(header['BUNIT'], format='fits').to_string()
        except (KeyError, ValueError):
            unit = None

        meta = OrderedDict(header)

        # parse astropy.io.fits.header._HeaderCommentaryCards as strings
        if 'HISTORY' in meta:
            meta['HISTORY'] = str(meta['HISTORY'])
        if 'COMMENT' in meta:
            meta['COMMENT'] = str(meta['COMMENT'])

        return cls(name, data, wcs, unit, meta)

    @classmethod
    def empty(cls, name=None, nxpix=200, nypix=200, binsz=0.02, xref=0, yref=0,
              fill=0, proj='CAR', coordsys='GAL', xrefpix=None, yrefpix=None,
              dtype='float64', unit=None, meta=None):
        """
        Create an empty image from scratch.

        Uses the same parameter names as the Fermi tool gtbin
        (see http://fermi.gsfc.nasa.gov/ssc/data/analysis/scitools/help/gtbin.txt).

        If no reference pixel position is given it is assumed to be
        at the center of the image.

        Parameters
        ----------
        name : str
            Name of the image.
        nxpix : int, optional
            Number of pixels in x axis. Default is 200.
        nypix : int, optional
            Number of pixels in y axis. Default is 200.
        binsz : float, optional
            Bin size for x and y axes in units of degrees. Default is 0.02.
        xref : float, optional
            Coordinate system value at reference pixel for x axis. Default is 0.
        yref : float, optional
            Coordinate system value at reference pixel for y axis. Default is 0.
        fill : float, optional
            Fill image with constant value. Default is 0.
        proj : string, optional
            Any valid WCS projection type. Default is 'CAR' (cartesian).
        coordsys : {'CEL', 'GAL'}, optional
            Coordinate system, either Galactic ('GAL') or Equatorial ('CEL').
            Default is 'GAL' (Galactic).
        xrefpix : float, optional
            Coordinate system reference pixel for x axis. Default is None.
        yrefpix: float, optional
            Coordinate system reference pixel for y axis. Default is None.
        dtype : str, optional
            Data type, default is float32
        unit : str
            Data unit.
        meta : `~collections.OrderedDict`
            Meta data attached to the image.

        Returns
        -------
        image : `~gammapy.image.SkyImage`
            Empty image.
        """
        header = make_header(nxpix, nypix, binsz, xref, yref,
                             proj, coordsys, xrefpix, yrefpix)
        data = fill * np.ones((nypix, nxpix), dtype=dtype)
        wcs = WCS(header)
        return cls(name=name, data=data, wcs=wcs, unit=unit, meta=header)

    @classmethod
    def empty_like(cls, image, name=None, unit=None, fill=0, meta=None):
        """
        Create an empty image like the given image.

        The WCS is copied over, the data array is filled with the ``fill`` value.

        Parameters
        ----------
        image : `~gammapy.image.SkyImage` or `~astropy.io.fits.ImageHDU`
            Instance of `~gammapy.image.SkyImage`.
        fill : float, optional
            Fill image with constant value. Default is 0.
        name : str
            Name of the image.
        unit : str
            String specifying the data units.
        meta : `~collections.OrderedDict`
            Dictionary to store meta data.
        """
        if isinstance(image, SkyImage):
            wcs = image.wcs.copy()
        elif isinstance(image, (fits.ImageHDU, fits.PrimaryHDU)):
            wcs = WCS(image.header)
        else:
            raise TypeError("Can't create image from type {}".format(type(image)))

        data = fill * np.ones_like(image.data)

        return cls(name, data, wcs, unit, meta=wcs.to_header())

    def fill(self, value):
        """
        Fill image with events.

        Parameters
        ----------
        value : float or `~gammapy.data.EventList`
             Value to fill in the map. If an event list is given, events will be
             binned in the map.
        """
        if isinstance(value, EventList):
            counts = _bin_events_in_cube(value, self.wcs, self.data.shape,
                                         origin=_DEFAULT_WCS_ORIGIN).sum(axis=0)
            self.data = counts.value
            self.unit = 'ct'
        elif np.isscalar(value):
            self.data.fill(value)
        else:
            raise TypeError("Can't fill value of type {}".format(type(value)))

    def write(self, filename, *args, **kwargs):
        """
        Write image to Fits file.

        Parameters
        ----------
        filename : str
            Name of the Fits file.
        *args : list
            Arguments passed to `~astropy.fits.ImageHDU.writeto`.
        **kwargs : dict
            Keyword arguments passed to `~astropy.fits.ImageHDU.writeto`.
        """
        hdu = self.to_image_hdu()
        hdu.writeto(filename, *args, **kwargs)

    def coordinates_pix(self, mode='center'):
        """
        Pixel sky coordinate images.

        Parameters
        ----------
        mode : {'center', 'edges'}
            Return coordinate values at the pixels edges or pixel centers.

        Returns
        -------
        x, y : tuple
            Return arrays representing the coordinates of a sky grid.
        """
        if mode == 'center':
            y, x = np.indices(self.data.shape)
        elif mode == 'edges':
            shape = self.data.shape[0] + 1, self.data.shape[1] + 1
            y, x = np.indices(shape)
            y, x = y - 0.5, x - 0.5
        else:
            raise ValueError('Invalid mode to compute coordinates.')

        return PixCoord(x, y)

    def coordinates(self, mode='center'):
        """
        Sky coordinate images.

        Parameters
        ----------
        mode : {'center', 'edges'}
            Return coordinate values at the pixels edges or pixel centers.

        Returns
        -------
        coordinates : `~astropy.coordinates.SkyCoord`
            Position on the sky.
        """
        pixcoord = self.coordinates_pix(mode=mode)
        coordinates = self.wcs_pixel_to_skycoord(xp=pixcoord.x, yp=pixcoord.y)
        return coordinates

    def contains(self, position):
        """
        Check if given position on the sky is contained in the image.

        Parameters
        ----------
        position : `~astropy.coordinates.SkyCoord`
            Position on the sky.

        Returns
        -------
        containment : array
            Bool array
        """
        ny, nx = self.data.shape
        x, y = self.wcs_skycoord_to_pixel(coords=position)
        return (x >= 0.5) & (x <= nx + 0.5) & (y >= 0.5) & (y <= ny + 0.5)

    def footprint(self, mode='corner'):
        """
        Footprint of the image on the sky.

        Parameters
        ----------
        mode : {'center', 'corner'}
            Use corner pixel centers or corners?

        Returns
        -------
        coordinates : `~collections.OrderedDict`
            Dictionary of the positions of the corners of the image
            with keys {'lower left', 'upper left', 'upper right', 'lower right'}
            and `~astropy.coordinates.SkyCoord` objects as values.

        Examples
        --------
        >>> from gammapy.image import SkyImage
        >>> image = SkyImage.empty(nxpix=3, nypix=2)
        >>> coord = image.footprint(mode='corner')
        >>> coord['lower left']
        <SkyCoord (Galactic): (l, b) in deg
            (0.03, -0.02)>
        """
        naxis2, naxis1 = self.data.shape

        if mode == 'center':
            pixcoord = [(0, 0), (0, naxis2), (naxis1, naxis2), (naxis1, 0)]
        elif mode == 'corner':
            pixcoord = [(-0.5, -0.5), (-0.5, naxis2 + 0.5),
                        (naxis1 + 0.5, naxis2 + 0.5), (naxis1 + 0.5, -0.5)]
        else:
            raise ValueError('Invalid mode: {}'.format(mode))

        coordinates = OrderedDict()
        keys = ['lower left', 'upper left', 'upper right', 'lower right']
        for key, (x, y) in zip(keys, pixcoord):
            coordinates[key] = self.wcs_pixel_to_skycoord(xp=x, yp=y)

        return coordinates

    def _get_boundaries(self, image_ref, image, wcs_check):
        """
        Get boundary coordinates of one image in the pixel coordinate system
        of another reference image.
        """
        ymax, xmax = image.data.shape
        ymax_ref, xmax_ref = image_ref.data.shape

        # transform boundaries in world coordinates
        bounds = image.wcs.wcs_pix2world([0, xmax], [0, ymax], _DEFAULT_WCS_ORIGIN)

        # transform to pixel coordinats in the reference image
        bounds_ref = image_ref.wcs.wcs_world2pix(bounds[0], bounds[1], _DEFAULT_WCS_ORIGIN)

        # round to nearest integer and clip at the boundaries
        xlo, xhi = np.rint(np.clip(bounds_ref[0], 0, xmax_ref))
        ylo, yhi = np.rint(np.clip(bounds_ref[1], 0, ymax_ref))

        if wcs_check:
            if not np.allclose(bounds_ref, np.rint(bounds_ref)):
                raise WcsError('World coordinate systems not aligned. Try to call'
                               ' .reproject() on one of the maps first.')

        return xlo, xhi, ylo, yhi

    def paste(self, image, method='sum', wcs_check=True):
        """
        Paste smaller image into image.

        WCS specifications of both images must be aligned. If not call
        `SkyImage.reproject()` on one of the maps first. See :ref:`image-cutpaste`
        more for information how to cut and paste sky images.

        Parameters
        ----------
        image : `~gammapy.image.SkyImage`
            Smaller image to paste.
        method : {'sum', 'replace'}, optional
            Sum or replace total values with cutout values.
        wcs_check : bool
            Check if both WCS are aligned. Raises `~astropy.wcs.WcsError` if not.
            Disable for performance critical computations.
        """
        xlo, xhi, ylo, yhi = self._get_boundaries(self, image, wcs_check)
        xlo_c, xhi_c, ylo_c, yhi_c = self._get_boundaries(image, self, wcs_check)

        if method == 'sum':
            self.data[ylo:yhi, xlo:xhi] += image.data[ylo_c:yhi_c, xlo_c:xhi_c]
        elif method == 'replace':
            self.data[ylo:yhi, xlo:xhi] = image.data[ylo_c:yhi_c, xlo_c:xhi_c]
        else:
            raise ValueError('Invalid method: {}'.format(method))

    def cutout(self, position, size):
        """
        Cut out rectangular piece of a image.

        See :ref:`image-cutpaste` for more information how to cut and paste
        sky images.

        Parameters
        ----------
        position : `~astropy.coordinates.SkyCoord`
            Position of the center of the image to cut out.
        size : int, array-like, `~astropy.units.Quantity`
            The size of the cutout array along each axis.  If ``size``
            is a scalar number or a scalar `~astropy.units.Quantity`,
            then a square cutout of ``size`` will be created.  If
            ``size`` has two elements, they should be in ``(ny, nx)``
            order.  Scalar numbers in ``size`` are assumed to be in
            units of pixels.  ``size`` can also be a
            `~astropy.units.Quantity` object or contain
            `~astropy.units.Quantity` objects.  Such
            `~astropy.units.Quantity` objects must be in pixel or
            angular units.  For all cases, ``size`` will be converted to
            an integer number of pixels, rounding the the nearest
            integer.  See the ``mode`` keyword for additional details on
            the final cutout size.

            .. note::
                If ``size`` is in angular units, the cutout size is
                converted to pixels using the pixel scales along each
                axis of the image at the ``CRPIX`` location.  Projection
                and other non-linear distortions are not taken into
                account.

        Returns
        -------
        cutout : `~gammapy.image.SkyImage`
            Cut out image.
        """
        cutout = Cutout2D(self.data, position=position, wcs=self.wcs, size=size,
                          copy=True)
        image = SkyImage(data=cutout.data, wcs=cutout.wcs, unit=self.unit)
        return image

    def pad(self, pad_width, mode='reflect', **kwargs):
        """
        Pad sky image at the edges.

        Calls `numpy.pad`, passing ``mode`` and ``kwargs`` to it and adapts the wcs
        specifcation.

        Parameters
        ----------
        pad_width : {sequence, array_like, int}
            Number of values padded to the edges of each axis, passed to `numpy.pad`
        mode : str ('reflect')
            Padding mode, passed to `numpy.pad`.


        Returns
        -------
        image : `~gammapy.image.SkyImage`
            Padded image

        Examples
        --------
        >>> from gammapy.image import SkyImage
        >>> image = SkyImage.empty(nxpix=10, nypix=13)
        >>> print(image.data.shape)
        (13, 10)
        >>> image2 = image.pad(pad_width=4, mode='reflect')
        >>> image2.data.shape
        (18, 21)
        """
        # converting from unicode to ascii string as a workaround
        # for https://github.com/numpy/numpy/issues/7112
        mode = str(mode)
        pad_width = _validate_lengths(self.data, pad_width)
        xlo, xhi = pad_width[self._ax_idx.x]
        ylo, yhi = pad_width[self._ax_idx.y]

        data = np.pad(self.data, pad_width=pad_width, mode=mode, **kwargs)

        wcs = self.wcs.deepcopy()
        wcs.wcs.crpix += np.array([xlo, ylo])

        return SkyImage(data=data, wcs=wcs)

    def crop(self, crop_width):
        """
        Crop sky image at the edges with given crop width.

        Analogous method to :meth:`SkyMap.pad()` to crop the sky image at the edges.
        Adapts the wcs specification accordingly.

        Parameters
        ----------
        crop_width : {sequence, array_like, int}
            Number of values cropped from the edges of each axis. Defined
            analogously to `pad_with` from `~numpy.pad`.

        Returns
        -------
        image : `~gammapy.image.SkyImage`
            Cropped image
        """
        crop_width = _validate_lengths(self.data, crop_width)
        xlo, xhi = crop_width[self._ax_idx.x]
        ylo, yhi = crop_width[self._ax_idx.y]

        data = self.data[ylo:-yhi, xlo:-xhi]

        wcs = self.wcs.deepcopy()
        wcs.wcs.crpix -= np.array([xlo, ylo])
        return SkyImage(data=data, wcs=wcs)

    def downsample(self, factor, method=np.nansum):
        """
        Down sample image by a given factor.

        The image is down sampled using `skimage.measure.block_reduce`. If the
        shape of the data is not divisible by the down sampling factor, the image
        must be padded beforehand to the correct shape.

        Parameters
        ----------
        factor : int
            Down sampling factor.
        method : np.ufunc (np.nansum), optional
            Method how to combine the image blocks.

        Returns
        -------
        image : `SkyImage`
            Down sampled image.
        """
        from skimage.measure import block_reduce

        shape = self.data.shape

        if not (np.mod(shape, factor) == 0).all():
            raise ValueError('Data shape {0} is not divisable by {1} in all axes.'
                             'Pad image prior to downsamling to correct'
                             ' shape.'.format(shape, factor))

        data = block_reduce(self.data, (factor, factor), method)

        # Adjust WCS
        wcs = get_resampled_wcs(self.wcs, factor, downsampled=True)
        return SkyImage(data=data, wcs=wcs)

    def upsample(self, factor, **kwargs):
        """
        Up sample image by a given factor.

        The image is up sampled using `scipy.ndimage.zoom`.

        Parameters
        ----------
        factor : int
            Up sampling factor.
        order : int
            Order of the interpolation usef for upsampling.

        Returns
        -------
        image : `SkyImage`
            Up sampled image.
        """
        from scipy.ndimage import zoom

        data = zoom(self.data, factor, **kwargs)

        # Adjust WCS
        wcs = get_resampled_wcs(self.wcs, factor, downsampled=False)
        return SkyImage(data=data, wcs=wcs)

    def lookup_max(self, region=None):
        """
        Find position of maximum in a image.

        Parameters
        ----------
        region : `~regions.SkyRegion` (optional)
            Limit lookup of maximum to that given sky region.

        Returns
        -------
        (position, value): `~astropy.coordinates.SkyCoord`, float
            Position and value of the maximum.
        """
        if region:
            mask = region.contains(self.coordinates())
        else:
            mask = np.ones_like(self.data)

        idx = np.nanargmax(self.data * mask)
        y, x = np.unravel_index(idx, self.data.shape)
        pos = self.wcs_pixel_to_skycoord(xp=x, yp=y)
        return pos, self.data[y, x]

    def solid_angle(self):
        """
        Solid angle image (2-dim `~astropy.units.Quantity` in `sr`).
        """

        coordinates = self.coordinates(mode='edges')
        lon = coordinates.data.lon.radian
        lat = coordinates.data.lat.radian

        # Compute solid angle using the approximation that it's
        # the product between angular separation of pixel corners.
        # First index is "y", second index is "x"
        ylo_xlo = lon[:-1, :-1], lat[:-1, :-1]
        ylo_xhi = lon[:-1, 1:], lat[:-1, 1:]
        yhi_xlo = lon[1:, :-1], lat[1:, :-1]

        dx = angular_separation(*(ylo_xlo + ylo_xhi))
        dy = angular_separation(*(ylo_xlo + yhi_xlo))
        omega = Quantity(dx * dy, 'sr')
        return omega

    def lookup(self, position, interpolation=None):
        """
        Lookup value at given sky position.

        Parameters
        ----------
        position : `~astropy.coordinates.SkyCoord`
            Position on the sky.
        interpolation : {'None'}
            Interpolation mode.
        """
        x, y = self.wcs_skycoord_to_pixel(coords=position)
        return self.data[np.rint(y).astype('int'), np.rint(x).astype('int')]

    def to_quantity(self):
        """
        Convert image to `~astropy.units.Quantity`.
        """
        return Quantity(self.data, self.unit)

    def to_sherpa_data2d(self, dstype='Data2D'):
        """
        Convert image to `~sherpa.data.Data2D` or `~sherpa.data.Data2DInt` class.

        Parameters
        ----------
        dstype : {'Data2D', 'Data2DInt'}
            Sherpa data type.
        """
        from sherpa.data import Data2D, Data2DInt

        if dstype == 'Data2D':
            coordinates = self.coordinates(mode='center')
            x = coordinates.data.lon
            y = coordinates.data.lat
            return Data2D(self.name, x.ravel(), y.ravel(), self.data.ravel(),
                          self.data.shape)
        elif dstype == 'Data2DInt':
            coordinates = self.coordinates(mode='edges')
            x = coordinates.data.lon
            y = coordinates.data.lat
            xlo, xhi = x[:-1], x[1:]
            ylo, yhi = y[:-1], y[1:]
            return Data2DInt(self.name, xlo.ravel(), xhi.ravel(),
                             ylo.ravel(), yhi.ravel(), self.data.ravel(),
                             self.data.shape)
        else:
            raise ValueError('Invalid sherpa data type.')

    def copy(self):
        """
        Copy image.
        """
        return deepcopy(self)

    def to_image_hdu(self):
        """
        Convert image to a `~astropy.io.fits.PrimaryHDU`.

        Returns
        -------
        hdu : `~astropy.io.fits.PrimaryHDU`
            Primary image hdu object.
        """
        if self.wcs is not None:
            header = self.wcs.to_header()
        else:
            header = fits.Header()

        # Add meta data
        header.update(self.meta)
        if self.unit is not None:
            header['BUNIT'] = Unit(self.unit).to_string('fits')
        if self.name is not None:
            header['EXTNAME'] = self.name
            header['HDUNAME'] = self.name
        return fits.PrimaryHDU(data=self.data, header=header)

    def reproject(self, reference, mode='interp', *args, **kwargs):
        """
        Reproject image to given reference.

        Parameters
        ----------
        reference : `~astropy.io.fits.Header`, or `~gammapy.image.SkyImage`
            Reference map specification to reproject the data on.
        mode : {'interp', 'exact'}
            Interpolation mode.
        *args : list
            Arguments passed to `~reproject.reproject_interp` or
            `~reproject.reproject_exact`.
        **kwargs : dict
            Keyword arguments passed to `~reproject.reproject_interp` or
            `~reproject.reproject_exact`.

        Returns
        -------
        image : `~gammapy.image.SkyImage`
            Skymap reprojected onto ``reference``.
        """

        from reproject import reproject_interp, reproject_exact

        if isinstance(reference, SkyImage):
            wcs_reference = reference.wcs
            shape_out = reference.data.shape
        elif isinstance(reference, fits.Header):
            wcs_reference = WCS(reference)
            shape_out = (reference['NAXIS2'], reference['NAXIS1'])
        else:
            raise TypeError("Invalid reference map must be either instance"
                            "of `Header`, `WCS` or `SkyImage`.")

        if mode == 'interp':
            out = reproject_interp((self.data, self.wcs), wcs_reference,
                                   shape_out=shape_out, *args, **kwargs)
        elif mode == 'exact':
            out = reproject_exact((self.data, self.wcs), wcs_reference,
                                  shape_out=shape_out, *args, **kwargs)
        else:
            raise TypeError("Invalid reprojection mode, either choose 'interp' or 'exact'")

        return SkyImage(name=self.name, data=out[0], wcs=wcs_reference,
                        unit=self.unit, meta=self.meta)

    def show(self, viewer='mpl', ds9options=None, **kwargs):
        """
        Show image in image viewer.

        Parameters
        ----------
        viewer : {'mpl', 'ds9'}
            Which image viewer to use. Option 'ds9' requires ds9 to be installed.
        ds9options : list, optional
            List of options passed to ds9. E.g. ['-cmap', 'heat', '-scale', 'log'].
            Any valid ds9 command line option can be passed.
            See http://ds9.si.edu/doc/ref/command.html for details.
        **kwargs : dict
            Keyword arguments passed to `~matplotlib.pyplot.imshow`.
        """
        if viewer == 'mpl':
            # TODO: replace by better MPL or web based image viewer
            import matplotlib.pyplot as plt
            fig = plt.gcf()
            axes = fig.add_axes([0.1, 0.1, 0.8, 0.8], projection=self.wcs)
            self.plot(axes, fig, **kwargs)
            plt.show()
        elif viewer == 'ds9':
            ds9options = ds9options or []
            with NamedTemporaryFile() as f:
                self.write(f)
                call(['ds9', f.name, '-cmap', 'bb'] + ds9options)
        else:
            raise ValueError("Invalid image viewer option, choose either"
                             " 'mpl' or 'ds9'.")

    def plot(self, ax=None, fig=None, **kwargs):
        """
        Plot image on matplotlib WCS axes.

        Parameters
        ----------
        ax : `~astropy.wcsaxes.WCSAxes`, optional
            WCS axis object to plot on.
        fig : `~matplotlib.figure.Figure`, optional
            Figure

        Returns
        -------
        ax : `~astropy.wcsaxes.WCSAxes`, optional
            WCS axis object
        fig : `~matplotlib.figure.Figure`, optional
            Figure
        """
        import matplotlib.pyplot as plt

        if fig is None and ax is None:
            fig = plt.gcf()
            ax = fig.add_subplot(1, 1, 1, projection=self.wcs)

        kwargs['origin'] = kwargs.get('origin', 'lower')
        kwargs['cmap'] = kwargs.get('cmap', 'afmhot')
        kwargs['interpolation'] = kwargs.get('interpolation', 'None')

        caxes = ax.imshow(self.data, **kwargs)
        unit = self.unit or 'A.U.'
        if unit == 'ct':
            quantity = 'counts'
        elif unit is 'A.U.':
            quantity = 'Unknown'
        else:
            quantity = Unit(unit).physical_type
        cbar = fig.colorbar(caxes, label='{0} ({1})'.format(quantity, unit))
        try:
            ax.coords['glon'].set_axislabel('Galactic Longitude')
            ax.coords['glat'].set_axislabel('Galactic Latitude')
        except KeyError:
            ax.coords['ra'].set_axislabel('Right Ascension')
            ax.coords['dec'].set_axislabel('Declination')
        except AttributeError:
            log.info("Can't set coordinate axes. No WCS information available.")

        return fig, ax

    def info(self):
        """
        Print summary info about the image.
        """
        print(str(self))

    def __str__(self):
        """
        String representation of the class.
        """
        info = "Name: {}\n".format(self.name)
        if self.data is not None:
            info += "Data shape: {}\n".format(self.data.shape)
            info += "Data type: {}\n".format(self.data.dtype)
            info += "Data unit: {}\n".format(self.unit)
            info += "Data mean: {:.3e}\n".format(np.nanmean(self.data))
        if self.wcs is not None:
            info += "WCS type: {}\n".format(self.wcs.wcs.ctype)
        return info

    def __array__(self):
        """
        Array representation of image.
        """
        return self.data

    def threshold(self, threshold):
        """
        Threshold sykmap data to create a `~gammapy.image.SkyMask`.

        Parameters
        ----------
        threshold : float
            Threshold value.

        Returns
        -------
        mask : `~gammapy.image.SkyMask`
            Exclusion mask object.

        Examples
        --------
        TODO: some more docs and example
        """
        from .mask import SkyMask
        mask = SkyMask.empty_like(self)
        mask.data = np.where(self.data > threshold, 0, 1)
        return mask

    def wcs_skycoord_to_pixel(self, coords):
        """
        Convert a set of SkyCoord coordinates into pixels.

        Calls `~astropy.wcs.utils.skycoord_to_pixel`, passing ``coords`` to it.

        Parameters
        ----------
        coords : `~astropy.coordinates.SkyCoord`
            The coordinates to convert.

        Returns
        -------
        xp, yp : `~numpy.ndarray`
            The pixel coordinates.
        """
        return skycoord_to_pixel(coords=coords, wcs=self.wcs,
                                 origin=_DEFAULT_WCS_ORIGIN,
                                 mode=_DEFAULT_WCS_MODE)

    def wcs_pixel_to_skycoord(self, xp, yp):
        """
        Convert a set of pixel coordinates into a `~astropy.coordinates.SkyCoord` coordinate.

        Calls `~astropy.wcs.utils.pixel_to_skycoord`, passing ``xp``, ``yp`` to it.

        Parameters
        ----------
        xp, yp : float or `~numpy.ndarray`
            The coordinates to convert.

        Returns
        -------
        coordinates : `~astropy.coordinates.SkyCoord`
            The celestial coordinates.

        Examples
        --------
        >>> from gammapy.image import SkyImage
        >>> image = SkyImage.empty(nxpix=10, nypix=15)
        >>> x, y = [5, 3.4], [8, 11.2]
        >>> image.wcs_pixel_to_skycoord(xp=x, yp=y)
        <SkyCoord (Galactic): (l, b) in deg
            [(359.99, 0.02), (0.022, 0.084)]>
        """
        return pixel_to_skycoord(xp=xp, yp=yp, wcs=self.wcs,
                                 origin=_DEFAULT_WCS_ORIGIN,
                                 mode=_DEFAULT_WCS_MODE)

    def wcs_pixel_scale(self, method='cdelt'):
        """Pixel scale.

        Returns angles along each axis of the image pixel at the CRPIX
        location once it is projected onto the plane of intermediate world coordinates.

        Calls `~astropy.wcs.utils.proj_plane_pixel_scales`.

        Parameters
        ----------
        method : {'cdelt', 'proj_plane'} (default 'cdelt')
            Result is calculated according to the 'cdelt' or 'proj_plane' methods.

        Returns
        -------
        angle : `~astropy.coordinates.Angle`
            An angle of projection plane increments corresponding to each pixel side (axis).

        Examples
        --------
        >>> from gammapy.image import SkyImage
        >>> image = SkyImage.empty(nxpix=3, nypix=2)
        >>> image.wcs_pixel_scale()
        <Angle [ 0.02, 0.02] deg>
        """
        if method == 'cdelt':
            scales = np.abs(self.wcs.wcs.cdelt)
        elif method == 'proj_plane':
            scales = proj_plane_pixel_scales(wcs=self.wcs)
        else:
            raise ValueError('Invalid method: {}'.format(method))

        return Angle(scales, unit='deg')

    def region_mask(self, region):
        """Create a boolean mask for a region.

        The ``data`` of this image is unchanged, a new mask is returned.

        The mask is:

        - ``True`` for pixels inside the region
        - ``False`` for pixels outside the region

        Parameters
        ----------
        region : `~regions.PixelRegion` or `~regions.SkyRegion` object
            A region on the sky could be defined in pixel or sky coordinates.

        Returns
        -------
        mask : `~gammapy.image.SkyMask`
            A boolean sky mask.

        Examples
        --------
        >>> from gammapy.image import SkyImage
        >>> from regions import CirclePixelRegion, PixCoord
        >>> region = CirclePixelRegion(center=PixCoord(x=2, y=1), radius=1.1)
        >>> image = SkyImage.empty(nxpix=5, nypix=4)
        >>> mask = image.region_mask(region)
        >>> print(mask.data.astype(int))
        [[0 0 1 0 0]
         [0 1 1 1 0]
         [0 0 1 0 0]
         [0 0 0 0 0]]
        """
        from .mask import SkyMask

        if isinstance(region, PixelRegion):
            coords = self.coordinates_pix()
        elif isinstance(region, SkyRegion):
            coords = self.coordinates()
        else:
            raise TypeError("Invalid region type, must be instance of "
                            "'regions.PixelRegion' or 'regions.SkyRegion'")

        data = region.contains(coords)

        return SkyMask(data=data, wcs=self.wcs)


class SkyImageCollection(Bunch):
    """
    Container for a collection of images.

    This class bundles as set of SkyMaps in single data container and provides
    convenience methods for Fits I/O and `~gammapy.extern.bunch.Bunch` like
    handling of the data members.

    Here's an example how to use it:

    .. code-block:: python

        from gammapy.image import SkyImageCollection
        images = SkyImageCollection.read('$GAMMAPY_EXTRA/datasets/fermi_survey/all.fits.gz')

    Then try tab completion on the ``images`` object.
    """
    # Real class attributes have to be defined here
    _map_names = []
    name = None
    meta = None
    wcs = None

    def __init__(self, name=None, wcs=None, meta=None, **kwargs):
        # Set real class attributes
        self.name = name
        self.wcs = wcs
        self.meta = meta

        # Everything else is stored as dict entries
        for key in kwargs:
            self[key] = kwargs[key]

    def __setitem__(self, key, item):
        """
        Overwrite __setitem__ operator to remember order the images are added
        to the collection, by storing it in the _map_names list.
        """
        if isinstance(item, np.ndarray):
            item = SkyImage(name=key, data=item, wcs=self.wcs)
        if isinstance(item, SkyImage):
            self._map_names.append(key)
        super(SkyImageCollection, self).__setitem__(key, item)

    @classmethod
    def read(cls, filename):
        """
        Create collection of images from Fits file.

        Parameters
        ----------
        filename : str
            Fits file name.
        """
        hdulist = fits.open(str(make_path(filename)))
        kwargs = {}
        _map_names = []  # list of map names to save order in fits file

        for hdu in hdulist:
            image = SkyImage.from_image_hdu(hdu)

            # This forces lower case map names, but only on the collection object
            # When writing to fits again the image.name attribute is used.
            name = image.name.lower()
            kwargs[name] = image
            _map_names.append(name)
        _ = cls(**kwargs)
        _._map_names = _map_names
        return _

    def write(self, filename=None, header=None, **kwargs):
        """
        Write Bunch of maps to Fits file.

        Parameters
        ----------
        filename : str
            Fits file name.
        header : `~astropy.io.fits.Header`
            Reference header to be used for all maps.
        """
        hdulist = fits.HDUList()
        for name in self.get('_map_names', sorted(self)):
            if isinstance(self[name], SkyImage):
                hdu = self[name].to_image_hdu()

                # For now add common collection meta info to the single map headers
                hdu.header.update(self.meta)
                hdu.name = name
                hdulist.append(hdu)
            else:
                log.warn("Can't save {} to file, not a image.".format(name))
        hdulist.writeto(filename, **kwargs)

    def info(self):
        """
        Print summary info about the image collection.
        """
        print(str(self))

    def __str__(self):
        """
        String representation of the image collection.
        """
        info = ''
        for name in self.get('_map_names', sorted(self)):
            info += self[name].__str__()
            info += '\n'
        return info
