# Licensed under a 3-clause BSD style license - see LICENSE.rst
from __future__ import absolute_import, division, print_function, unicode_literals
from astropy.tests.helper import pytest
from astropy.io import fits
from astropy.tests.helper import assert_quantity_allclose 
from astropy.coordinates import SkyCoord, Angle
from regions import CircleSkyRegion
from ..reflected import find_reflected_regions
from ...image import SkyMask
from ...datasets import gammapy_extra
from ...utils.testing import requires_data, requires_dependency


@pytest.fixture
def mask():
    """Example mask for testing."""
    testfile = gammapy_extra.filename('datasets/exclusion_masks/tevcat_exclusion.fits')
    hdu = fits.open(testfile)[1]
    return SkyMask.from_image_hdu(hdu)


@requires_dependency('scipy')
@requires_data('gammapy-extra')
def test_find_reflected_regions(mask):
    pos = SkyCoord(80.2, 23.5, unit='deg', frame='icrs')
    radius = Angle(0.4, 'deg')
    region = CircleSkyRegion(pos, radius)
    center = SkyCoord(83.2, 22.7, unit='deg', frame='icrs')
    regions = find_reflected_regions(region, center, mask,
                                     min_distance_input=Angle('0 deg'))
    assert (len(regions)) == 20
    assert_quantity_allclose(regions[3].center.icrs.ra,
                             Angle('81.752 deg'),
                             rtol=1e-2)
    
