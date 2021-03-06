# similarity.py --- Simularity measures between protein ensembles
# Copyright (C) 2014 Wouter Boomsma, Matteo Tiberti
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""
Ensemble similarity calculations --- :mod:`encore.similarity`
=====================================================================

The module contains implementations of similary measures between
protein ensembles described in:

     Similarity Measures for Protein Ensembles. Lindorff-Larsen, K.; 
     Ferkinghoff-Borg, J. PLoS ONE 2009, 4, e4203.

"""
import optparse
import numpy
import warnings
from time import sleep
from MDAnalysis import Universe
from Ensemble import Ensemble
from clustering.Cluster import ClustersCollection
from clustering.affinityprop import AffinityPropagation
from dimensionality_reduction.stochasticproxembed import StochasticProximityEmbedding, kNNStochasticProximityEmbedding
from confdistmatrix import MinusRMSDMatrixGenerator, RMSDMatrixGenerator
from covariance import covariance_matrix, EstimatorShrinkage, EstimatorML
from multiprocessing import cpu_count
from utils import *
from scipy.stats import gaussian_kde
from random import randint

# Silence deprecation warnings - scipy problem
warnings.filterwarnings("ignore", category=DeprecationWarning) 
warnings.filterwarnings("ignore", category=RuntimeWarning) 
warnings.filterwarnings("ignore", category=FutureWarning) 



# Low boundary value for log() argument - ensure no nans 
EPSILON=1E-15

# x*log(y) with the assumption that 0*(log(0)) = 0
xlogy = numpy.vectorize(lambda x,y : 0.0 if (x<=EPSILON and y<=EPSILON) else x*numpy.log(y))     

# discrete dKL
def discrete_kullback_leibler_divergence(pA, pB):
    """Kullback-Leibler divergence between discrete probability distribution. Notice that since this measure is not symmetric  :math:`d_{KL}(p_A,p_B) != d_{KL}(p_B,p_A)`

    **Arguments:**
	
	`pA` : iterable of floats
		First discrete probability density function
	
	`pB` : iterable of floats
		Second discrete probability density function

    **Returns:**
	
	`dkl` : float
		Discrete Kullback-Liebler divergence
	"""

    return numpy.sum( xlogy(pA, pA/pB) )

# discrete dJS
def discrete_jensen_shannon_divergence(pA, pB):
    """Jensen-Shannon divergence between discrete probability distributions.

    **Arguments:**
        
	`pA` : iterable of floats
                First discrete probability density function
        
	`pB` : iterable of floats
                Second discrete probability density function

    **Returns:**
        
	`djs` : float
                Discrete Jensen-Shannon divergence
"""
    return 0.5*( discrete_kullback_leibler_divergence(pA, (pA+pB)*0.5) + 
                 discrete_kullback_leibler_divergence(pB, (pA+pB)*0.5) )

# calculate harmonic similarity
def harmonic_ensemble_similarity(ensemble1=None,
                                 ensemble2=None,
                                 sigma1=None,
                                 sigma2=None,
                                 x1=None,
                                 x2=None,
                                 mass_weighted=True,
                                 covariance_estimator = EstimatorShrinkage()):
    ''' 
    Calculate the harmonic ensemble similarity measure
    as defined in 

	    Similarity Measures for Protein Ensembles. Lindorff-Larsen, K.; 
	    Ferkinghoff-Borg, J. PLoS ONE 2009, 4, e4203.

    **Arguments:**

	`ensemble1` : encore.Ensemble or None
		First ensemble to be compared. If this is None, sigma1 and x1 must be provided.

	`ensemble2` : encore.Ensemble or None
		Second ensemble to be compared. If this is None, sigma2 and x2 must be provided.

	`sigma1` : numpy.array
		Covariance matrix for the first ensemble. If this None, calculate it from ensemble1 using covariance_estimator

	`sigma2` : numpy.array
		Covariance matrix for the second ensemble. If this None, calculate it from ensemble1 using covariance_estimator

	`x1`: numpy.array 
		Mean for the estimated normal multivariate distribution of the first ensemble. If this is None, calculate it from ensemble1

	`x2`: numpy.array
                Mean for the estimated normal multivariate distribution of the first ensemble.. If this is None, calculate it from ensemble2

	`mass_weighted` : bool
		Whether to perform mass-weighted covariance matrix estimation

	`covariance_estimator` : either EstimatorShrinkage or EstimatorML objects
		Which covariance estimator to use
	
    **Returns:**
	
	`dhes` : float
		harmonic similarity measure
    '''

    # If matrices and means are specified, use them
    if x1 == None or x2 == None or sigma1 == None or sigma2 == None:
        if ensemble1 == None or ensemble2 == None:
            raise RuntimeError

        # Extract coordinates from ensembles
        coordinates_system1 = ensemble1.coordinates
        coordinates_system2 = ensemble2.coordinates
    
        # Average coordinates in the two systems
        x1 = numpy.average(coordinates_system1, axis=0).flatten()
        x2 = numpy.average(coordinates_system2, axis=0).flatten()

        # Covariance matrices in the two systems
        sigma1 = covariance_matrix(ensemble1, 
                               mass_weighted=mass_weighted,
                               estimator = covariance_estimator)
        sigma2 = covariance_matrix(ensemble2, 
                               mass_weighted=mass_weighted,
                               estimator = covariance_estimator)

    # Inverse covariance matrices
    sigma1_inv = numpy.linalg.pinv(sigma1)
    sigma2_inv = numpy.linalg.pinv(sigma2)

    # Difference between average vectors
    d_avg = x1 - x2

    # Sigma
    sigma = sigma1_inv + sigma2_inv

    # Distance measure
    trace = numpy.trace(numpy.dot(sigma1, sigma2_inv) + 
                        numpy.dot(sigma2, sigma1_inv)
                        - 2*numpy.identity(sigma1.shape[0]))

    d_hes = 0.25*(numpy.dot(numpy.transpose(d_avg), 
                            numpy.dot(sigma1_inv + sigma2_inv,
                                      d_avg)) + trace)
    return d_hes

def clustering_ensemble_similarity(cc, ens1, ens1_id, ens2, ens2_id):
    """Clustering ensemble similarity: calculate the probability densities from the clusters and calculate discrete Jensen-Shannon divergence.
	
	**Arguments:**

	`cc` : encore.ClustersCollection 
		Collection from cluster calculated by a clustering algorithm (e.g. Affinity propagation)
	
	`ens1` : encore.Ensemble
		First ensemble to be used in comparison
	
        `ens2` : encore.Ensemble
                Second ensemble to be used in comparison
		
	`ens1_id` : int
		First ensemble id as detailed in the ClustersCollection metadata

	`ens2_id` : int
		Second ensemble id as detailed in the ClustersCollection metadata

	**Returns:**

	`djs` : float
		Jensen-Shannon divergence between the two ensembles, as calculated by the clustering ensemble similarity method
	"""
    tmpA = numpy.array( [ numpy.where(c.metadata['ensemble'] == ens1_id)[0].shape[0]/float(ens1.coordinates.shape[0]) for c in cc ] )
    tmpB = numpy.array( [ numpy.where(c.metadata['ensemble'] == ens2_id)[0].shape[0]/float(ens2.coordinates.shape[0]) for c in cc ] )
                    
    # Exclude clusters which have 0 elements in both ensembles    
    pA=tmpA[tmpA+tmpB > EPSILON]
    pB=tmpB[tmpA+tmpB > EPSILON]

    return discrete_jensen_shannon_divergence(pA, pB)

def cumulative_clustering_ensemble_similarity(cc, ens1, ens1_id, ens2, ens2_id, ens1_id_min=1, ens2_id_min=1):
    """ Calculate clustering ensemble similarity between joined ensembles. This means that, after clustering has been performed, some ensembles are merged and the dJS is calculated between the probability distributions of the two clusters groups. In particular, the two ensemble groups are defined by their ensembles id: one of the two joined ensembles will comprise all the ensembles with id [ens1_id_min, ens1_id], and the other ensembles will comprise all the ensembles with id [ens2_id_min, ens2_id].

**Arguments:**

        `cc` : encore.ClustersCollection
                Collection from cluster calculated by a clustering algorithm (e.g. Affinity propagation)

        `ens1` : encore.Ensemble
                First ensemble to be used in comparison

        `ens2` : encore.Ensemble
                Second ensemble to be used in comparison

        `ens1_id` : int
                First ensemble id as detailed in the ClustersCollection metadata

        `ens2_id` : int
                Second ensemble id as detailed in the ClustersCollection metadata

        **Returns:**

        `djs` : float
                Jensen-Shannon divergence between the two ensembles, as calculated by the clustering ensemble similarity method

"""

    ensA = [ numpy.where( numpy.logical_and(c.metadata['ensemble'] <= ens1_id, c.metadata['ensemble']) >= ens1_id_min)[0].shape[0] for c in cc ]
    ensB = [ numpy.where( numpy.logical_and(c.metadata['ensemble'] <= ens2_id, c.metadata['ensemble']) >= ens2_id_min)[0].shape[0] for c in cc ]
    sizeA = float(numpy.sum(ensA))
    sizeB = float(numpy.sum(ensB))
    #sizeA = float( numpy.sum( [numpy.where( numpy.logical_and(c.metadata['ensemble'] <= ens1_id, c.metadata['ensemble']) >= ens1_id_min)[0].shape[0] for c in cc])
    #sizeB = float(numpy.sum( [numpy.where( numpy.logical_and(c.metadata['ensemble'] <= ens2_id, c.metadata['ensemble']) >= ens2_id_min)[0].shape[0] for c in cc])

    tmpA = numpy.array( ensA )/sizeA
    tmpB = numpy.array( ensB  )/sizeB

    # Exclude clusters which have 0 elements in both ensembles
    pA=tmpA[tmpA+tmpB > EPSILON]
    pB=tmpB[tmpA+tmpB > EPSILON]

    return discrete_jensen_shannon_divergence(pA, pB)

def gen_kde_pdfs(embedded_space, ensemble_assignment, nensembles,  nsamples=None, **kwargs):
    """ 
    Generate Kernel Density Estimates (KDE) from embedded spaces and elaborate the coordinates for later use.

**Arguments:**

`embedded_space` : numpy.array
	Array containing the coordinates of the embedded space

`ensemble_assignment` : numpy.array
	Array containing one int per ensemble conformation. These allow to distinguish, in the complete embedded space, which conformations belong to each ensemble. For instance if ensemble_assignment is [1,1,1,1,2,2], it means that the first four conformations belong to ensemble 1 and the last two to ensemble 2

`nesensembles` : int
	Number of ensembles

`nsamples` : int samples to be drawn from the ensembles. Will be required in a later stage in order to calculate dJS.`

**Returns:**

`kdes` : scipy.stats.gaussian_kde
	KDEs calculated from ensembles

`resamples` : list of numpy.array
	For each KDE, draw samples according to the probability distribution of the KDE mixture model

`embedded_ensembles` : list of numpy.array
	List of numpy.array containing, each one, the elements of the embedded space belonging to a certain ensemble
"""
    kdes = []
    embedded_ensembles = []
    resamples = []
    
    for i in range(1,nensembles+1):
        this_embedded = embedded_space.transpose()[numpy.where(ensemble_assignment == i)].transpose()
        embedded_ensembles.append(this_embedded)
        kdes.append(gaussian_kde(this_embedded)) # XXX support different bandwidth values

    # Set number of samples
    if not nsamples:
        nsamples = this_embedded.shape[1]*10

    # Resample according to probability distributions
    for this_kde in kdes:
        resamples.append(this_kde.resample(nsamples))

    return (kdes, resamples, embedded_ensembles)
    
def dimred_ensemble_similarity(kde1, resamples1, kde2, resamples2, ln_P1_exp_P1=None, ln_P2_exp_P2=None, ln_P1P2_exp_P1=None, ln_P1P2_exp_P2=None):
    """ Calculate the Jensen-Shannon divergence according the the Dimensionality reduction method. In this case, we have continuous probability densities we have to integrate over the measureable space. Our target is calculating Kullback-Liebler, which is defined as:

.. math::
	D_{KL}(P(x) || Q(x)) = \\int_{-\\infty}^{\\infty}P(x_i) ln(P(x_i)/Q(x_i)) = \\langle{}ln(P(x))\\rangle{}_P - \\langle{}ln(Q(x))\\rangle{}_P

where the :math:`\\langle{}.\\rangle{}_P` denotes an expectation calculated under the 
distribution P. We can, thus, just estimate the expectation values of the components to get an estimate of dKL.
Since the Jensen-Shannon distance is actually  more complex, we need to estimate four expectation values:

.. math::	
     \\langle{}log(P(x))\\rangle{}_P

     \\langle{}log(Q(x))\\rangle{}_Q

     \\langle{}log(0.5*(P(x)+Q(x)))\\rangle{}_P

     \\langle{}log(0.5*(P(x)+Q(x)))\\rangle{}_Q

**Arguments:**

`kde1` : scipy.stats.gaussian_kde
	Kernel density estimation for ensemble 1

`resamples1` : numpy.array
	Samples drawn according do kde1. Will be used as samples to calculate the expected values according to 'P' as detailed before.

`kde2` : scipy.stats.gaussian_kde
        Kernel density estimation for ensemble 2

`resamples2` : numpy.array
        Samples drawn according do kde2. Will be used as sample to calculate the expected values according to 'Q' as detailed before.	

`ln_P1_exp_P1` : float or None
	Use this value for :math:`\\langle{}log(P(x))\\rangle{}_P`; if None, calculate it instead

`ln_P2_exp_P2` : float or None
        Use this value for :math:`\\langle{}log(Q(x))\\rangle{}_Q`; if None, calculate it instead

`ln_P1P2_exp_P1` : float or None
        Use this value for :math:`\\langle{}log(0.5*(P(x)+Q(x)))\\rangle{}_P`;  if None, calculate it instead

`ln_P1P2_exp_P1` : float or None
        Use this value for :math:`\\langle{}log(0.5*(P(x)+Q(x)))\\rangle{}_Q`; if None, calculate it instead	

**Returns:**

`djs` : float
	Jensen-Shannon divergence calculated according to the dimensionality reduction method 
"""

    if not ln_P1_exp_P1 and not ln_P2_exp_P2 and not ln_P1P2_exp_P1 and not ln_P1P2_exp_P2:
        ln_P1_exp_P1 = numpy.average(numpy.log(kde1.evaluate(resamples1)))
        ln_P2_exp_P2 = numpy.average(numpy.log(kde2.evaluate(resamples2)))
        ln_P1P2_exp_P1 = numpy.average(numpy.log(0.5*(kde1.evaluate(resamples1)+kde2.evaluate(resamples1))))
        ln_P1P2_exp_P2 = numpy.average(numpy.log(0.5*(kde1.evaluate(resamples2)+kde2.evaluate(resamples2))))

    return 0.5 * (ln_P1_exp_P1 - ln_P1P2_exp_P1 + ln_P2_exp_P2 - ln_P1P2_exp_P2)

def cumulative_gen_kde_pdfs(embedded_space, ensemble_assignment, nensembles,  nsamples=None, ens_id_min=1, ens_id_max=None):
    """
    Generate Kernel Density Estimates (KDE) from embedded spaces and elaborate the coordinates for later use. However, consider more than one ensemble as the space on which the KDE will be generated. In particular, will use ensembles with ID [ens_id_min, ens_id_max]. 

**Arguments:**

`embedded_space` : numpy.array
        Array containing the coordinates of the embedded space

`ensemble_assignment` : numpy.array
        array containing one int per ensemble conformation. These allow to distinguish, in the complete embedded space, which conformations belong to each ensemble. For instance if ensemble_assignment is [1,1,1,1,2,2], it means that the first four conformations belong to ensemble 1 and the last two to ensemble 2

`nesensembles` : int
        Number of ensembles

`nsamples : int 
	Samples to be drawn from the ensembles. Will be required in a later stage in order to calculate dJS.`

`ens_id_min` : int 
	Minimum ID of the ensemble to be considered; see description

`ens_id_max` : int
	Maximum ID of the ensemble to be considered; see description

**Returns:**

`kdes` : scipy.stats.gaussian_kde
        KDEs calculated from ensembles

`resamples` : list of numpy.array
        For each KDE, draw samples according to the probability distribution of the kde mixture model

`embedded_ensembles` : list of numpy.array
        List of numpy.array containing, each one, the elements of the embedded space belonging to a certain ensemble
    """

    kdes = []
    embedded_ensembles = []
    resamples = []
    if not ens_id_max:
        ens_id_max = nensembles+1
    for i in range(ens_id_min, ens_id_max+1):
        this_embedded = embedded_space.transpose()[numpy.where(numpy.logical_and(ensemble_assignment >= ens_id_min, ensemble_assignment <= i))].transpose()
        embedded_ensembles.append(this_embedded)
        kdes.append(gaussian_kde(this_embedded)) # XXX support different bandwidth values

    # Set number of samples
    if not nsamples:
        nsamples = this_embedded.shape[1]*10

    # Resample according to probability distributions
    for this_kde in kdes:
        resamples.append(this_kde.resample(nsamples))

    return (kdes, resamples, embedded_ensembles)

def write_output(matrix, base_fname=None, header="", suffix="", extension="dat"):
    """
    Write output matrix with a nice format, to stdout and optionally a file.  

**Arguments:**

`matrix` : encore.utils.TriangularMatrix
        Matrix containing the values to be printed

`base_fname` : str
	Basic filename for output. If None, no files will be written, and the matrix will be just printed on screen

`header` : str
        Line to be written just before the matrix

`suffix` : str 
	String to be concatenated to basename, in order to get the final file name        

`extension` : str 
	Extension for the output file       

    """

    if base_fname != None:
        fname = base_fname+"-"+suffix+"."+extension
    else:
        fname = None
    matrix.square_print(header=header, fname=fname)
        
def write_output_line(value, fhandler=None, suffix="", label="win.", number=0, rawline=None):
    """
    Write a line of data with a fixed format to standard output and optionally file. The line will be appended or written to a file object.
The format is (in the Python str.format specification language): '{:s}{:d}\t{:.3f}', with the first element being the label, the second being
a number that identifies the data point, and the third being the number itself. For instance:

win.3	0.278

**Arguments:**

`value` : float
        Value to be printed.

`fhandler` : file object
	File object in which the line will be written. if None, nothing will be written to file, and the value will be just printed on screen

`label` : str
        Label to be written before the data 

`number` : int 
	Number that identifies the data being written in this line.        

`rawline` : str
	If rawline is not None, write rawline to fhandler instead of the formatted number line. rawline can be any arbitrary string.       
    """

    if fhandler == None:
        fh = Tee(sys.stdout)
    else:
        fh = Tee(sys.stdout, fhandler)

    if rawline != None:
        print >>fh, rawline
        return

    print >>fh, "{:s}{:d}\t{:.3f}".format(label, number, value)

def bootstrap_coordinates(coords, times):
    """
    Bootstrap conformations in a encore.Ensemble. This means drawing from the encore.Ensemble.coordinates numpy array with replacement "times" times and returning the outcome. 

**Arguments:**

`coords` : numpy.array
        3-dimensional coordinates array

`times` : int
        number of times the coordinates will be bootstrapped

**Returns:**

`out` : list
        Bootstrapped coordinates list. len(out) = times.
    """
    out = []
    for t in range(times):
        this_coords=numpy.zeros(coords.shape)
        for c in range(this_coords.shape[0]):
            this_coords[c,:,:] = coords[numpy.random.randint(low=0, high=this_coords.shape[0]),:,:]
        out.append(this_coords)
    return out

def bootstrap_matrix(matrix):
    """
    Bootstrap an input square matrix. The resulting matrix will have the same shape as the original one, but the order of its elements will be drawn (with repetition). Separately bootstraps each ensemble.

**Arguments:**

`matrix` : encore.utils.TriangularMatrix
        similarity/dissimilarity matrix

**Returns:**

`this_m` : encore.utils.TriangularMatrix
        bootstrapped similarity/dissimilarity matrix
    """
    ensemble_identifiers = numpy.unique(ensemble_assignment)
    this_m = TriangularMatrix(size = matrix.size)
    indexes = []
    for ens in ensemble_identifiers:
        old_indexes = numpy.where(ensemble_assignment == ens)[0]
        indexes.append( numpy.random.randint(low=numpy.min(old_indexes), high=numpy.max(old_indexes)+1, size=old_indexes.shape[0] ) )

    indexes = numpy.hstack(indexes)
    for j in range(this_m.size):
        for k in range(j):
            this_m[j, k] = matrix[indexes[j], indexes[k]]
        
    logging.info("Matrix bootstrapped.")
    return this_m
    



if __name__ == "__main__":
    
    import optparse
    import logging

    group_templates = OptionGroups()

# Main definitions
    group = group_templates.add_group("Main options")
    group.add_option("--nensembles", dest="nensembles", default=2, type="int",
                      help="Number of ensembles to compare (default: 2)")
    group.add_option("--mode", dest="mode", default="harmonic", type="choice",
                      choices=["hes","harmonic", "ces", "clustering","dres", "dimred"],
                      help="Ensemble similarity method (default: harmonic)")
    group.add_option("--np", dest="coresn", default=cpu_count(), type=int,
                      help="Maximum number of processes to perform calculation (default: as many as the system's cores (%d))"% cpu_count())
    group.add_option("--no-align", dest="align", action="store_false", default=True,
                      help="Whether to align ensembles to the reference conformation before calculating similarity. Reference conformation will be the topology, if not specified otherwise with --reference.")
    group.add_option("--reference", dest="reference", default=None,
                 help="Reference conformation to which conformations will be aligned, if desired.")
    group.add_option("--topology", dest="topology", type="string",
                 help="Topology file for ensemble %(index)s. Supported formats: PDB, PDBQT, PQR, GRO, CRD")
    group.add_option("--details", dest="details", type="string", default=None,
                 help="Store details on the performed calculations in file. If several calculations have been performed with different parameters a bunch of files will be generated, one for each calculation.")
    group.add_option("-v","--verbose", dest="verbose", action="store_true", default=False,
                      help="Toggle verbose mode")
    group.add_option("--evaluate-convergence", dest="evaluate_convergence", action="store_true", default=False,
                 help="Use the ensemble comparison measure to evaluate the convergence of the ensemble 1.")
    group.add_option("--evaluate-convergence-mode", dest="convergence_mode", type="choice", default="increasing-window", choices=["increasing-window"],
                     help="Compare a time-window of increasing size with the rest of the trajectory. (default: increasing-window)")
    group.add_option("--estimate-error", dest="estimate_error", action="store_true", default=False,
                 help="Estimate error")
    group.add_option("--estimate-error-mode", dest="error_mode", type="choice", default="bootstrapping", choices=["bootstrapping"], 
                 help="Method with which the error will be estimated (default: bootstrapping)")
    group.add_option("--output-files","-o", dest="outfiles", action="store", type="str", default=None,
                     help="Write single matrices in output files as well. Use this basename for the file names.")

# Options for evaluate-convergence=half-half
    #group = group_templates.add_group("evaluate-convergence-mode=half-half options")
    #group.add_option("--window-size", dest="window_size", type=int, default=2500,
    #                 help="Size of used windows (number of frames; default 2500)")

    group = group_templates.add_group("estimate-error-mode=bootstrapping options")
    group.add_option("--bootstrapping-runs", dest="bootstrapping_runs", type=int, default=1000,
                     help="Number of bootstrapping runs (default: 1000)")

    group = group_templates.add_group("evaluate-convergence-mode=increasing-window options")
    group.add_option("--window-size", dest="window_size", type=int, default=2500,
                     help="Size of used windows (number of frames; default 2500)")

    #group = group_templates.add_group("evaluate-convergence-mode=increasing-half options")
    #group.add_option("--window-size", dest="window_size", type=int, default=2500,
    #                 help="Size of used windows (number of frames; default 2500)")

# Options for mode=harmonic 
    group = group_templates.add_group("mode=harmonic options")
    group.add_option("--covariance-estimator", type="choice", dest="covariance_estimator", default="shrinkage",
                      choices=["ml","shrinkage"],
                      help="Type of covariance matrix estomator (maximum likelihood (ml) or shrinkage (default: shrinkage)")

# Options for mode=cluster 
    group = group_templates.add_group("mode=clustering options")
    group.add_option("--similarity-mode", dest="similarity_mode", default="minusrmsd", type="choice",
                      choices=["minusrmsd"],
                      help="Metric for distance matrix calculation (default: minusrmsd)")
    group.add_option("--clustering-mode", dest="clustering_mode", default="ap", type="choice",
                      choices=["ap"],
                      help="Clustering algorithm to be used, [ap: Affinity Propagation] (default: ap)")
                  
# Options for mode=dimred
    group = group_templates.add_group("mode=dimred options")
    group.add_option("--similarity-mode", dest="similarity_mode", default="rmsd", type="choice",
                      choices=["rmsd"],
                      help="Metric for similarity matrix calculation (defaukt: rmsd)")
    group.add_option("--dimred-mode", dest="dimred_mode", default="spe", type="choice", choices=["spe"],
                      help="Dimensionality reduction method (default: spe)" )
    group.add_option("--density-mode", dest="density_mode", default="kde", type="choice",
                      choices=["kde"],
                      help="Density estimation method (default: kde)")
    group.add_option("--dim", dest="dim", default="2", type="str",
                      help="Dimensionality of the embedded spaces (one or more, comma-separated: 2,3,4; default: 2)")

    #group.add_option("--replicas", dest="replicas", default=1, type="int",
    #                  help="Number of replicas for each number of dimensions")

# Options for dimred-mode = spe
    group = group_templates.add_group("dimred-mode=spe options")
    group.add_option("--spe-mode", dest="spe_mode", default='vanilla',type='choice',
                      choices=['vanilla','rn','knn'],
		      help="Types of spe calculation [plain SPE (vanilla), k-Nearest neighbours SPE (knn), Random neighborhood SPE (rn)] (default: vanilla)0")
    group.add_option("--neighborhood-cutoff", dest="neighborhood_cutoff", default=1.5, type="float",
                      help="Neighborhood cutoff (vanilla; default: 1.5)")
    group.add_option("--nneighs", dest="kn", default=15, type="int",
                      help="number of neighbours to be considered (knn and rn; default: 15)")
    group.add_option("--max-lambda", dest="maxlam", default=2.0, type="float",
                      help="Starting lambda learning rate parameter (default: 2.0)")
    group.add_option("--min-lambda", dest="minlam", default=0.1, type="float",
                      help="Final lambda learning rate (default: 0.1)")
    group.add_option("--nsteps", dest="nstep", default=100, type="int",
                      help="Number of steps per cycle (default: 50)")
    group.add_option("--ncycles", dest="ncycle", default=50, type="int",
                      help="Number of cycles per run. At the end of every cycle, lambda is changed. (default: 50)")
    group.add_option("--stress-frequency", dest="stressfreq", default=-1, type="int",
                      help="Calculate residual stress value every --stress-frequency cycle (default: -1 (never))")

# Options for ensembles
    group = group_templates.add_group("Ensemble %(index)s options")
    group.add_option("--ensemble%(index)s-trajectory", dest="ensemble%(index)s_trajectory", type="string",
                 help="Trajectory file for ensemble %(index)s. Supported formats: DCD, XTC, TRR, XYZ, TRJ, MDCRD, PDB")
#    group.add_option("--ensemble%(index)s-start", dest="ensemble%(index)s_start", type="int", default=0,
#                 help="Start index for ensemble %(index)s")
#    group.add_option("--ensemble%(index)s-end", dest="ensemble%(index)s_end", type="int", default=None,
#                 help="End index for ensemble %(index)s")
    group.add_option("--ensemble%(index)s-frame-interval", dest="ensemble%(index)s_frame_interval", type="int", default=1,
                 help="Frame interval ensemble %(index)s (default: 1)")
    group.add_option("--ensemble%(index)s-atom-selection", dest="ensemble%(index)s_atom_selection_string", default="(name CA)",
                 help="CHARMM-style atom selection (default: name CA)")
    
# Options for similarity-mode=minusrmsd
    group = group_templates.add_group("similarity-mode=minusrmsd options")
    group.add_option("--superimpose", dest="superimpose", action="store_true", default = False,
                      help="Whether to superimpose structures before calculating distance")
    group.add_option("--superimposition-subset", dest="superimposition_subset", default = None,
                      help="Group for superimposition (MDAnalysis selection syntax). Otherwise, the whole structure, as defined by --atom-selection, will be used.")
    group.add_option("--no-mass-weighted", dest="mass_weighted", action="store_false", default = True,
                      help="Calculate non-massweighted RMSD (also, superimposition will not be mass-weighted)")
    group.add_option("--save-matrix", "--save-similarity-matrix", dest="save_matrix", default = None,
                      help="Save calculated similarity/dissimilarity matrix as numpy binary file. A filename is required.")
    group.add_option("--load-matrix", "--load-similarity-matrix", dest="load_matrix", default = None,
                      help="Load similarity/dissimilarity matrix from numpy binary file instead of calculating it. A filename is required.")
    group.add_option("--change-matrix-sign", dest="change_matrix_sign", default=False, action="store_true", help="Change the sign of the elements of loaded matrix")
    group.add_option("--matrix-only", dest="matrix_only", default=False, action="store_true",
                      help="Calculate (and save) the similarity matrix only. Run together with --save-matrix") 

    # Options for similarity-mode=rmsd
    group = group_templates.add_group("similarity-mode=rmsd options")
    group.add_option("--superimpose", dest="superimpose", action="store_true", default = False,
                      help="Whether to superimpose structures before calculating distance")
    group.add_option("--superimposition-subset", dest="superimposition_subset", default = None,
                      help="Group for superimposition (MDAnalysis selection syntax). Otherwise, the whole structure, as defined by --atom-selection, will be used.")
    group.add_option("--no-mass-weighted", dest="mass_weighted", action="store_false", default = True,
                      help="Calculate non-massweighted RMSD (also, superimposition will not be mass-weighted)")
    group.add_option("--save-matrix", dest="save_matrix", default = None,
                      help="Save calculated matrix as numpy binary file. A filename is required.")
    group.add_option("--load-matrix", dest="load_matrix", default = None,
                      help="Load matrix from numpy binary file instead of calculating it. A filename is required.")
    group.add_option("--change-matrix-sign", dest="change_matrix_sign", default=False, action="store_true", help="Invert the sign of the elements of the loaded matrix")
    group.add_option("--matrix-only", dest="matrix_only", default=False, action="store_true",
                      help="Calculate (and save) the similarity matrix only. Run together with --save-matrix") 

# Options for similarity-mode=ap
    group = group_templates.add_group("clustering-mode=ap options")
    group.add_option("--preferences", dest="preferences", default="-5.0", type="str",
                      help="Preference values, comma-separated (default: -5.0")    
    group.add_option("--lambda", dest="lam", default=0.8, type="float",
                      help="Damping factor ([0.0;1.0] (default: 0.5))")
    group.add_option("--maxiter", dest="max_iterations", default=1000, type="int",
                      help="Maximum number of iterations (default: 1000)")
    group.add_option("--convergence", dest="convergence", default=50, type="int",
                      help="Minimum number of unchanging iterations to achieve convergence (default: 50)")
    group.add_option("--nonoise", dest="noise", action="store_false", default=True,
                      help="Do not add noise to data (note: similarities must be not degenerate!)")

# Options for density_mode = kde
    group = group_templates.add_group("density-mode=kde options")
#    group.add_option("--bw-method", dest="bw_method", default="scott", type="choice",
#                      choices=['scott','silverman','scalar'], help="number of nearest neighbours to each element")
    group.add_option("--use-density", dest='use_density', default='grid', type="choice",
                      choices=['grid','data','resample'], help="Compute JS divergence by evaluating density on the selected points")
    group.add_option("--grid-resolution", dest="kde_resolution", default="0.01", type="float",
                       help="Grid resolution for Kernel Density Estimation (default: 0.01)"), 
    #group.add_option("--grid-size", dest="grid_size", default=1.0, type="float",
    #                help="For each dimension, grid size will be chosen as (max-min)+2*(max-min)*D.")
    group.add_option("--samples", dest="samples", default=1000, type="int",
                       help="Number of points to resample from kde (default: 1000)"), 

    usage = """%prog [options].

    Since many options are useful only when using one of the three
    ensemble similarity methods, many of them are hidden by default.
    In order to show them, please run similarity.py -h together with:

    --mode=hes: options for the harmonic similarity method (default)    
    --mode=ces: options for the clustering ensemble similarity method
    --mode=dres: options for the dimensionality reduction ensemble 
		 similarity method

    for instance:
	
	similarity.py --mode=clustering -h
"""

    ##### Parse command line options
    parser = optparse.OptionParser(usage=usage)
    group_main = optparse.OptionGroup(parser, "Main Options")

    group_cluster = optparse.OptionGroup(parser, "mode=clustering options")

    # Parsing phase 1 
    option_groups = [group_templates["Main options"]]
    parser_phase1 = ParserPhase(option_groups, allow_unrecognized=True, add_help_option=False)
    parser_phase1.parse()
    
    # Parsing phase 2 

    if parser_phase1.options.mode == "harmonic" or parser_phase1.options.mode == "hes":
        option_groups += [group_templates["mode=harmonic options"]]
    elif parser_phase1.options.mode == "clustering" or parser_phase1.options.mode == "ces":
        option_groups += [group_templates["mode=clustering options"]]
    elif parser_phase1.options.mode == "dimred" or parser_phase1.options.mode == "dres":
        option_groups += [group_templates["mode=dimred options"]]

    if parser_phase1.options.evaluate_convergence:
        if parser_phase1.options.convergence_mode == "increasing-window":
            option_groups += [group_templates["evaluate-convergence-mode=increasing-window options"]]

    if parser_phase1.options.estimate_error:
        if parser_phase1.options.error_mode == "bootstrapping":
            option_groups += [group_templates["estimate-error-mode=bootstrapping options"]]
        #if parser_phase1.options.convergence_mode == "half-half":
        #    option_groups += [group_templates["evaluate-convergence-mode=half-half options"]]
        #elif parser_phase1.options.convergence_mode == "increasing-half":
        #    option_groups += [group_templates["evaluate-convergence-mode=increasing-half options"]]

    
    option_groups += [group_templates["Ensemble %(index)s options"].duplicate(i+1) for i in range(parser_phase1.options.nensembles)]
    
    parser_phase2 = ParserPhase(option_groups, allow_unrecognized=True, add_help_option=False)
    
    parser_phase2.parse()
        
    # Parsing phase 3
    if parser_phase2.options.mode == "clustering" or parser_phase2.options.mode == "ces":
        if parser_phase2.options.similarity_mode == "minusrmsd":
            option_groups += [group_templates["similarity-mode=minusrmsd options"]]
        if parser_phase2.options.clustering_mode == "ap":
            option_groups += [group_templates["clustering-mode=ap options"]]
    elif parser_phase2.options.mode == "dimred" or parser_phase2.options.mode == "dres":
        if parser_phase2.options.similarity_mode == "rmsd":
            option_groups += [group_templates["similarity-mode=rmsd options"]]
        if parser_phase2.options.dimred_mode == "spe":
            option_groups += [group_templates["dimred-mode=spe options"]]
        if parser_phase2.options.density_mode == "kde":
            option_groups += [group_templates["density-mode=kde options"]]

    parser_phase3 = ParserPhase(option_groups, allow_unrecognized=False, add_help_option=True, usage=usage)
    parser_phase3.parse()

    # Set logging level and format
 
    #console = logging.StreamHandler()
    #logging.addHandler(console)    

    if parser_phase3.options.verbose:
        #logging.basicConfig(format='%(asctime)s %(message)s', datefmt='%y-%m-%d %H:%M:%S : ',level=logging.INFO)
        #logging.basicConfig(format='%(asctime)s.%(msecs)03d',datefmt='%Y-%m-%d,%H:%M:%S', level=logging.INFO)
        logging.basicConfig(format='%(asctime)s.%(msecs)03d : %(message)s', datefmt='%Y-%m-%d,%H:%M:%S', level=logging.INFO)
    else:
        logging.basicConfig(level=logging.WARNING)
        #logging.basicConfig(format='%(asctime)s %(message)s', datefmt='%y-%m-%d %H:%M:%S : ',level=logging.WARNING)
    
    #logging.Formatter()
    logging.basicConfig(format='%(asctime)s.%(msecs)03d : %(message)s', datefmt='%Y-%m-%d,%H:%M:%S', level=logging.INFO)

    logging.info("Loading ensembles . . .")    
    
    ensembles = []
    ensemble_numbers = []


    # Check if topology file has been specified
    if not parser_phase3.options.topology:    
        parser_phase3.parser.error("Topology file not specified.")
        exit(1)

    # Check if evaluate convergence. In this case, just 1 ensemble.

    if parser_phase3.options.evaluate_convergence:
        if parser_phase3.options.nensembles > 1:
            logging.warning("WARNING: only ensemble 1 will be considered for convergence evaluation.")
            parser_phase3.options.nensembles = 1

    # Check if the number of ensemble_trajectorys is consistent with the desired number of ensembles
    for i in range(1,parser_phase3.options.nensembles+1):
        if getattr(parser_phase3.options, "ensemble%d_trajectory"%i):
            ensemble_numbers.append(i)
    if set(range(1,parser_phase3.options.nensembles+1)) != set(ensemble_numbers):
        parser_phase3.parser.error("ERROR: Wrong number of ensembles or trajectories specified.")
        exit(1)

    # Load ensembles
    for i in range(1,parser_phase3.options.nensembles+1):
        trajectories = getattr(parser_phase3.options, "ensemble%d_trajectory"%i).split(",")
        atom_selection_string = getattr(parser_phase3.options, "ensemble%d_atom_selection_string"%i)
        frame_interval =  getattr(parser_phase3.options, "ensemble%d_frame_interval"%i)
        try:
            superimposition_subset_string = parser_phase3.options.superimposition_subset
        except:
            parser_phase3.options.superimposition_subset = None
        ensembles.append( Ensemble(topology = parser_phase3.options.topology,
                                   trajectory = trajectories,
                                   atom_selection_string = atom_selection_string,
                                   superimposition_selection_string = parser_phase3.options.superimposition_subset,
                                   frame_interval = frame_interval ) )
                                   
        logging.info("""Ensemble %d Loaded.
    trajectories: %s
    frame interval: %d
    number of frames: %d
    atoms selection: %s
    number of atoms: %d\n""" % (i, "\n".ljust(19).join(trajectories), frame_interval, len(ensembles[-1].coordinates), atom_selection_string, len(ensembles[-1].coordinates[0]) ) )

    #Check if the ensembles contain the same number of atoms
    coordinatesn = len(ensembles[0].coordinates[0])
    for e in ensembles[1:]:
        if len(e.coordinates[0]) != coordinatesn:
            logging.error("ERROR: ensembles must contain the same number of atoms.")
            exit(1)
            
    logging.info("Done! %d ensembles loaded." % len(ensembles))

    # If required, align to reference before proceeding. 
    if parser_phase3.options.align:
        if not parser_phase3.options.reference:
            reference = parser_phase3.options.topology
            logging.info("Performing least-square fit of each frame on the topology conformation.")
        else:
            reference = parser_phase3.options.reference
            logging.info("Performing least-square fit of each frame on the user-specified reference conformation.")

        reference_universe = Universe(parser_phase3.options.topology, 
                                                 reference)
        for e in ensembles:
            e.align(reference_universe)
    else:
        logging.info("Not performing preliminar least-square superimposition.")
        
    # Calculate the number of matrix elements output and create the matrix. The diagonal is not considered.
    out_matrix_eln = parser_phase3.options.nensembles 
    values = TriangularMatrix(size = out_matrix_eln)

    # Generate ensemble pair indeces for this calculation
    pairs_indeces = [k for k in trm_indeces_nodiag(parser_phase3.options.nensembles)]    

    logging.info("Similarity metric calculations will now begin. %d values will be computed." % out_matrix_eln)
    logging.info("%d core(s) will be used for parallel calculations." % parser_phase3.options.coresn)

    # If convergence: splice ensembles
    if parser_phase3.options.evaluate_convergence:

        ens_size = ensembles[0].coordinates.shape[0]
        
        slices_n = [0]

        tmp_ensembles = []

        #if parser_phase3.options.convergence_mode == 'half-half': #or parser_phase3.options.convergence_mode == 'sliding-window' or parser_phase3.options.convergence_mode == 'fixed-window':
            #if parser_phase3.options.convergence_mode == 'half-half':
                #first_window_size = ens_size/2
                #if ens_size % first_window_size == 0:
                    #parser_phase3.options.window_size = first_window_size
                #else:
                    #parser_phase3.options.window_size = first_window_size + 1
            #elif parser_phase3.options.convergence_mode == 'sliding-window':
            #    first_window_size = parser_phase3.options.window_size
            #elif parser_phase3.options.convergence_mode == 'fixed-window':
            #    first_window_size = parser_phase3.options.first_window_size
        
            #slices_n.append(first_window_size)
        
            #rest_slices = (ens_size - first_window_size)/parser_phase3.options.window_size
            #print "r_s", rest_slices

            #residuals =  (ens_size - first_window_size) % parser_phase3.options.window_size

            #for rs in range(rest_slices):
                #slices_n.append(slices_n[-1] + parser_phase3.options.window_size)
            #if residuals != 0:
                #slices_n.append(slices_n[-1] + residuals)
                #logging.warning("WARNING: the last window will be shorter than the prescribed window size (%s frames)"%residuals)
            
                #tmp_ensembles = []
            #for s in range(len(slices_n)-1):
                #tmp_ensembles.append( Ensemble(topology = parser_phase3.options.topology,
                #                           trajectory = parser_phase3.options.topology,
                #                           atom_selection_string = atom_selection_string,
                #                           superimposition_selection_string = parser_phase3.options.superimposition_subset,
                #                           frame_interval = frame_interval ) )
            
                #tmp_ensembles[-1].coordinates = ensembles[0].coordinates[slices_n[s]:slices_n[s+1],:,:]        
        
        if parser_phase3.options.convergence_mode == "increasing-window": #or parser_phase3.options.convergence_mode=="increasing-window":

            window_size = parser_phase3.options.window_size
            #if parser_phase3.options.convergence_mode == "increasing-half":
            #    ref_window_size = ens_size/2
            
            ref_window_size = 0
            if ref_window_size % window_size != 0:
                ref_window_size += ref_window_size % window_size
            rest_slices = (ens_size - ref_window_size) / parser_phase3.options.window_size
            residuals = (ens_size - ref_window_size) % parser_phase3.options.window_size
                
            for rs in range(rest_slices-1):
                slices_n.append(slices_n[-1] + parser_phase3.options.window_size)
            if residuals != 0:
                slices_n.append(slices_n[-1] + residuals + parser_phase3.options.window_size)
                logging.warning("WARNING: the last window will be shorter than the prescribed window size (%s frames)"%residuals)
            else:
                slices_n.append(slices_n[-1] + parser_phase3.options.window_size)
            
                
            for s in range(len(slices_n)-1):
                tmp_ensembles.append( Ensemble(topology = parser_phase3.options.topology,
                                               trajectory = parser_phase3.options.topology,
                                               atom_selection_string = atom_selection_string,
                                               superimposition_selection_string = parser_phase3.options.superimposition_subset,
                                               frame_interval = frame_interval ) )
                #print slices_n
                tmp_ensembles[-1].coordinates = ensembles[0].coordinates[slices_n[s]:slices_n[s+1],:,:]
            
            if ref_window_size > 0:
                tmp_ensembles.append( Ensemble(topology = parser_phase3.options.topology,
                                               trajectory = parser_phase3.options.topology,
                                               atom_selection_string = atom_selection_string,
                                               superimposition_selection_string = parser_phase3.options.superimposition_subset,
                                               frame_interval = frame_interval ) )
                tmp_ensembles[-1].coordinates = ensembles[0].coordinates[slices_n[-1]:,:,:]
            if parser_phase3.options.convergence_mode == "increasing-half":
                ref_ensemble = tmp_ensembles[-1]
            else:
                ref_ensemble = ensembles[0]
            
        ensembles = tmp_ensembles 
        parser_phase3.options.nensembles = len(ensembles)
    
    if parser_phase3.options.mode == "harmonic" or parser_phase3.options.mode == "hes":
        logging.info("Chosen metric: Harmonic similarity")
        if out_matrix_eln % parser_phase3.options.coresn != 0:
            logging.warning("WARNING: for optimal performance, the number of cores should be a factor of the number of similarity metric values.")
        if parser_phase3.options.covariance_estimator == "shrinkage":
            covariance_estimator = EstimatorShrinkage()
            logging.info("    Covariance matrix estimator: Shrinkage")
        else:
            covariance_estimator = EstimatorML()
            logging.info("    Covariance matrix estimator: Maximum Likelihood")
        
        xs = []
        sigmas = []

        if parser_phase1.options.estimate_error:
            if parser_phase1.options.error_mode == "bootstrapping":
                data = []
                for t in range(parser_phase3.options.bootstrapping_runs):
                    logging.info("The coordinates will be bootstrapped.")
                    xs = []
                    sigmas = []
                    values = numpy.zeros((out_matrix_eln,out_matrix_eln))
                    for e in ensembles:
                        this_coords = bootstrap_coordinates(e.coordinates, 1)[0]
                        xs.append(numpy.average(this_coords, axis=0).flatten())
                        sigmas.append( covariance_matrix(e,
                                                         mass_weighted=True,
                                                         estimator = covariance_estimator) )
                    for i,j in pairs_indeces:
                        value = harmonic_ensemble_similarity(x1 = xs[i],
                                                             x2 = xs[j],
                                                             sigma1 = sigmas[i],
                                                             sigma2 = sigmas[j])
                        values[i,j] = value
                        values[j,i] = value
                    data.append(values)
                outs = numpy.array(data)
                avgs = np.average(data, axis=0)
                stds = np.std(data, axis=0)

                print "averages:"
                print_square_array(avgs)
                print "standard deviations:"
                print_square_array(stds)
                print "values (ens.A-ens.B value_run_1 value_run_2 ... value_run_N):"
                for i,j in pairs_indeces:
                    print "%d-%d\t" % (i+1,j+1),
                    print " ".join(["%.3f"%k for k in outs[:,i,j]])
                exit(0)

        # Calculate the parameters for the multivariate normal distribution of each ensemble
        for e in ensembles:
            
            # Extract coordinates from each ensemble
            coordinates_system = e.coordinates
    
            # Average coordinates in each system
            xs.append(numpy.average(coordinates_system, axis=0).flatten())

            # Covariance matrices in each system
            sigmas.append( covariance_matrix(e, 
                               mass_weighted=True,
                               estimator = covariance_estimator) )
                        
        if parser_phase3.options.evaluate_convergence:
            fname = str(parser_phase3.options.outfiles)+"_convergence_"
            #if parser_phase3.options.convergence_mode == 'half-half':
            #    fname+="half-half_harmonic.dat"
            #    if parser_phase3.options.outfiles != None:
            #        fhandler = open(fname,'a')
            #    else:
            #        fhandler = None
            #    write_output_line(0, rawline="=== half vs half convergence estimation ===", fhandler=fhandler)
            #    write_output_line(0, rawline="half-half hes: %.3f" %   
            #                                            harmonic_ensemble_similarity(x1 = xs[0],
            #                                              x2 = xs[1],
            #                                              sigma1 = sigmas[0],
            #                                              sigma2 = sigmas[1]), 
            #                                              fhandler=fhandler)
                
            #elif parser_phase3.options.convergence_mode == 'sliding-window':
            #    print "=== sliding window convergence estimation ==="
            #    for i in range(len(ensembles)-1):
            #        print "%.3f" % harmonic_ensemble_similarity(x1 = xs[i],
            #                                                   x2 = xs[i+1],
            #                                                   sigma1 = sigmas[i],
            #                                                   sigma2 = sigmas[i+1])

            #elif parser_phase3.options.convergence_mode == 'sliding-window':
            #    print "=== sliding window convergence estimation ==="
            #    for i in range(1,len(ensembles)):
            #        print "%.3f" % harmonic_ensemble_similarity(x1 = xs[0],
            #                                                   x2 = xs[i],
            #                                                   sigma1 = sigmas[0],
            #                                                   sigma2 = sigmas[i])            

            #if parser_phase3.options.convergence_mode == "increasing-half":
            #    fname += "increasing-half_harmonic.dat"
            #    if parser_phase3.options.outfiles != None:
            #        fhandler = open(fname,'a')
            #    else:
            #        fhandler = None
	    #
            #    ref_x = numpy.average(ref_ensemble.coordinates, axis=0).flatten()
            #    ref_sigma = covariance_matrix(ref_ensemble,
            #                                  mass_weighted=True,
            #                                  estimator = covariance_estimator) 
            #    write_output_line(rawline="# === first half vs increasing window convergence estimation ===", fhandler=fhandler)
            #    for i in range(0,len(ensembles[:-1])):
            #        write_output_line(harmonic_ensemble_similarity(x1 = ref_x,
            #                                                    x2 = xs[i],
            #                                                    sigma1 = ref_sigma,
            #                                                    sigma2 = sigmas[i]),
            #        fhandler = fhandler,
            #        number = i+1)

            if parser_phase3.options.convergence_mode == "increasing-window":
                fname += "increasing-window_harmonic.dat"
                if parser_phase3.options.outfiles != None:
                    fhandler = open(fname,'a')
                else:
                    fhandler = None

                ref_x = numpy.average(ref_ensemble.coordinates, axis=0).flatten()
                ref_sigma = covariance_matrix(ref_ensemble,
                                              mass_weighted=True,
                                              estimator = covariance_estimator)

                write_output_line(0,rawline="# === Increasing window convergence estimation ===")
                for i in range(0,len(ensembles)):
                    write_output_line(harmonic_ensemble_similarity(x1 = ref_x,
                                                                x2 = xs[i],
                                                                sigma1 = ref_sigma,
                                                                sigma2 = sigmas[i]),
                    fhandler = fhandler,
                    number = i+1)

        else:
            for i,j in pairs_indeces:
                values[i,j] = harmonic_ensemble_similarity(x1 = xs[i],
                                                           x2 = xs[j],
                                                           sigma1 = sigmas[i],
                                                           sigma2 = sigmas[j])
        # Save details as required
        if parser_phase3.options.details:
            kwds = {}
            for i in range(len(ensembles)):
                kwds['ensemble%d_mean'%(i+1)] = xs[i]
                kwds['ensemble%d_covariance_matrix'%(i+1)] = sigmas[i]
            numpy.savez(parser_phase3.options.details, **kwds)       


        header = "# === Harmonic similarity ==="
        write_output(values, header=header, base_fname=parser_phase3.options.outfiles, suffix="harmonic")
        
        logging.info("Calculation complete.")
        exit(0)

    if parser_phase3.options.mode == "clustering" or parser_phase3.options.mode == "ces":
        logging.info("Chosen metric: Conformational clustering")
    if parser_phase3.options.mode == "dimred" or parser_phase3.options.mode == "dres":
        logging.info("Chosen metric: Dimensionality reduction")
    if parser_phase3.options.mode == "clustering" or parser_phase3.options.mode == "dimred" or parser_phase3.options.mode == "ces" or parser_phase3.options.mode == "dres": # safeguard 

        trajlist = []
        ensemble_assignment = []

        # Define ensemble assignments as required on the joined ensemble
        for i in range(1, parser_phase3.options.nensembles+1):
            ensemble_assignment += [i for j in ensembles[i-1].coordinates]
        ensemble_assignment = numpy.array(ensemble_assignment)
        #print ensemble_assignment

        # Joined ensemble
        joined_ensemble = Ensemble(topology=parser_phase3.options.topology,
                                   trajectory=[parser_phase3.options.topology],
                                   atom_selection_string = parser_phase3.options.ensemble1_atom_selection_string,
                                   superimposition_selection_string = parser_phase3.options.superimposition_subset)

        # Joined ensemble coordinates as a concatenation of single ensembles - faster this way
        joined_ensemble.coordinates = numpy.concatenate(tuple([ e.coordinates for e in ensembles ]) )
        joined_ensemble.superimposition_coordinates = numpy.concatenate(tuple([ e.superimposition_coordinates for e in ensembles ]) )
       
        # Define metadata dictionary
        metadata = {'ensemble': ensemble_assignment}
        
        # Choose distance metric
        if parser_phase3.options.similarity_mode == "minusrmsd":
            logging.info("    Similarity matrix: -RMSD matrix")
            matrix_builder = MinusRMSDMatrixGenerator()
        elif parser_phase3.options.similarity_mode == "rmsd":
            logging.info("    Similarity matrix: RMSD matrix")
            matrix_builder = RMSDMatrixGenerator()

        # Load the matrix if required
        if parser_phase3.options.load_matrix:
            logging.info("        Loading similarity matrix from: %s"%parser_phase3.options.load_matrix)
            confdistmatrix = TriangularMatrix(size=joined_ensemble.coordinates.shape[0], loadfile=parser_phase3.options.load_matrix)
            logging.info("        Done!")
            for key in confdistmatrix.metadata.dtype.names:
                logging.info("        %s : %s" % (key, str(confdistmatrix.metadata[key][0])) )

            # Change matrix sign if required. Useful to switch between similarity/distance matrix.
            if parser_phase3.options.change_matrix_sign:
                logging.info("        The matrix sign will be changed.")
                for k,v in enumerate(confdistmatrix._elements):
                    confdistmatrix._elements[k] = -v

            # Check matrix size for consistency
            if not confdistmatrix.size == joined_ensemble.coordinates.shape[0]:
                logging.error("ERROR: The size of the loaded matrix and of the ensemble do not match")
                exit(1)

        # Calculate the matrix  
        else:
            logging.info("        Perform pairwise alignment: %s"       % str(parser_phase3.options.superimpose))
            logging.info("        Mass-weighted alignment and RMSD: %s" % str(parser_phase3.options.mass_weighted))
            if parser_phase3.options.superimpose:
                logging.info("        Atoms subset for alignment: %s" % parser_phase3.options.superimposition_subset ) 
            logging.info("    Calculating similarity matrix . . .")

            # Use superimposition subset, if necessary. If the pairwise alignment is not required, it will not be performed anyway.
            if parser_phase3.options.superimposition_subset:
                confdistmatrix = matrix_builder(joined_ensemble, 
                                    pairwise_align = parser_phase3.options.superimpose, 
                                    align_subset_coordinates = joined_ensemble.superimposition_coordinates,
                                    mass_weighted = parser_phase3.options.mass_weighted,
                                    ncores = parser_phase3.options.coresn)

            else:
                confdistmatrix = matrix_builder(joined_ensemble, 
                                    pairwise_align = parser_phase3.options.superimpose, 
                                    mass_weighted = parser_phase3.options.mass_weighted,
                                    ncores = parser_phase3.options.coresn)
                                
            logging.info("    Done!")
            if parser_phase3.options.save_matrix:
                logging.info("    Similarity matrix will be saved in %s.%s"%(parser_phase3.options.save_matrix, "" if parser_phase3.options.save_matrix[-3:] == "npz" else "npz"))
                confdistmatrix.savez(parser_phase3.options.save_matrix)
            if parser_phase3.options.matrix_only:
                logging.info("The calculation of the simialrity matrix only was requested; calculation finished.")
                exit(0)

        if parser_phase3.options.estimate_error: # if bootstrap
            logging.info("Error will be estimated instead of normal calculation.")
            if parser_phase3.options.error_mode == "bootstrapping":
                logging.info("Error estimation mode: Bootstrapping")
                logging.info("the similarity matrix will be bootstrapped %d times." % parser_phase3.options.bootstrapping_runs)

                bs_args = [tuple([confdistmatrix]) for i in range(parser_phase3.options.bootstrapping_runs)]

                pc = ParallelCalculation(parser_phase3.options.coresn, bootstrap_matrix, bs_args)
                
                pc_results = pc.run()
                bootstrap_matrices = zip(*pc_results)[1]
                
    # Start building Probability density functions (pdf)
    if parser_phase3.options.mode == "clustering" or parser_phase3.options.mode == "ces":

        # Clustering mode
        if parser_phase3.options.clustering_mode == "ap":
            
            preferences = map(float,parser_phase3.options.preferences.split(","))
                                    
            logging.info("    Clustering algorithm: Affinity Propagation")
            logging.info("        Preference values: %s" % ", ".join(map(lambda x: "%3.2f"%x ,preferences)))
            logging.info("        Maximum iterations: %d" % parser_phase3.options.max_iterations)
            logging.info("        Convergence: %d" % parser_phase3.options.convergence)
            logging.info("        Damping: %1.2f"%  parser_phase3.options.lam)
            logging.info("        Apply noise to similarity matrix: %s" % str(parser_phase3.options.noise))
 
            if len(preferences) % parser_phase3.options.coresn != 0:
                logging.warning("WARNING: for optimal performance, the number of cores should be a factor of the number of preference values.")

            # Choose clustering algorithm
            clustalgo = AffinityPropagation()

            # Prepare input for parallel calculation
            if parser_phase3.options.estimate_error:
                if parser_phase3.options.error_mode == "bootstrapping":
                    confdistmatrixs = []
                    lams = []
                    max_iterationss = []
                    convergences = []
                    noises = []
                    real_prefs = []
                    for p in preferences: 
                        confdistmatrixs.extend(bootstrap_matrices)
                        lams.extend([parser_phase3.options.lam]*len(bootstrap_matrices))
                        max_iterationss.extend([parser_phase3.options.max_iterations]*len(bootstrap_matrices))
                        noises.extend([parser_phase3.options.noise]*len(bootstrap_matrices))
                        convergences.extend([parser_phase3.options.convergence]*len(bootstrap_matrices))
                        real_prefs.extend([p]*len(bootstrap_matrices))
                    old_prefs = preferences
                    preferences = real_prefs
            else:
                confdistmatrixs = [ confdistmatrix for i in preferences ]
                lams = [ parser_phase3.options.lam for i in preferences ]
                max_iterationss = [ parser_phase3.options.max_iterations for i in preferences ]
                convergences = [ parser_phase3.options.convergence for i in preferences ]
                noises = [ int(parser_phase3.options.noise) for i in preferences ]

            args = zip(confdistmatrixs, preferences, lams, max_iterationss, convergences, noises)
            logging.info("    Starting affinity propagation runs . . .")

            # Do it
            pc = ParallelCalculation(parser_phase3.options.coresn, clustalgo, args)

            results = pc.run()
            
            logging.info("\n    Done!")

            # Create clusters collections from clustering results, one for each cluster. None if clustering didn't work.
            ccs = [ ClustersCollection(clusters[1], metadata=metadata) for clusters in results ]
            
        
            if parser_phase3.options.estimate_error:
                if parser_phase3.options.error_mode == "bootstrapping":
                    preferences = old_prefs
                    k = 0
                    for i,p in enumerate(preferences):
                        failed_runs = 0
                        values = []
                        for j in range(parser_phase3.options.bootstrapping_runs):
                            if ccs[k].clusters == None:
                                failed_runs += 1
                                k += 1
                                continue
                            values.append(numpy.zeros((out_matrix_eln,out_matrix_eln)))
            
                            for pair in pairs_indeces:
                                # Calculate dJS
                                this_djs = clustering_ensemble_similarity( ccs[k], ensembles[pair[0]], pair[0]+1, ensembles[pair[1]], pair[1]+1 )
                                values[-1][pair[0],pair[1]] = this_djs
                                values[-1][pair[1],pair[0]] = this_djs
                            k += 1
                        outs = numpy.array(values)
                        avgs = numpy.average(outs, axis=0)
                        stds = numpy.std(outs, axis=0)

                        print "== clustering ensemble similarity, preference %.1f" %p
                        print "averages:"
                        print_square_array(avgs)
                        print "standard deviations:"
                        print_square_array(stds)
                        print "values (ens.A-ens.B value_run_1 value_run_2 ... value_run_N):"
                        for i,j in pairs_indeces:
                            print "%d-%d\t" % (i+1,j+1),
                            print " ".join(["%.3f"%k for k in outs[:,i,j]])

                exit(0)

            for i,p in enumerate(preferences):
                if ccs[i].clusters == None:
                    continue
                if parser_phase3.options.evaluate_convergence:
                    fname = str(parser_phase3.options.outfiles)+"_convergence_clustering_preference%.1f" %p
                    header = "=== convergence clustering, preference %.1f, "%p

                    if parser_phase3.options.convergence_mode=="increasing-window":
                        values = TriangularMatrix(size=out_matrix_eln)
                        fname += "_increasing-window.dat"
                        header += "increasing window ==="

                        if parser_phase3.options.outfiles != None:
                            fhandler = open(fname,'a')
                        else:
                            fhandler = None

                        write_output_line(0,rawline=header, fhandler=fhandler)
                        for j in range(0,len(ensembles)):
                            write_output_line( cumulative_clustering_ensemble_similarity( ccs[i], 
                                                                                        ensembles[-1], 
                                                                                        len(ensembles)+1,
                                                                                        ensembles[j], j+1),
                            fhandler=fhandler,
                            number=j+1)
                # for every preference value


                else:
                    values = TriangularMatrix(size=out_matrix_eln)

                    header = "# ==== Preference value: %1.2f ==="%p
                    for pair in pairs_indeces:
                    # Calculate dJS
                        values[pair[0],pair[1]] = clustering_ensemble_similarity( ccs[i], ensembles[pair[0]], pair[0]+1, ensembles[pair[1]], pair[1]+1)
                
                    write_output(values, header=header, base_fname=parser_phase3.options.outfiles, suffix="clustering-pref%.1f"%p)
                        
                if parser_phase3.options.details:
                    kwds = {}
                    kwds['centroids'] = numpy.array([c.centroid for c in ccs[i]])
                    kwds['ensemble_sizes'] = numpy.array([e.coordinates.shape[0] for e in ensembles])
                    for cln,cluster in enumerate(ccs[i]):
                        kwds["cluster%d"%(cln+1)] = numpy.array(cluster.elements)
                    numpy.savez("%s_preference_%.2f"%(parser_phase3.options.details,p), **kwds)

            logging.info("Calculation complete.")
            exit(0)

    if parser_phase3.options.mode == "dimred" or parser_phase3.options.mode == "dres":
        dimensions = map(int,parser_phase3.options.dim.split(','))

        # prepare runs. (e.g.: runs = [1,2,3,1,2,3,1,2,3, ...])
        if parser_phase3.options.estimate_error:
            if parser_phase3.options.error_mode == "bootstrapping":
                runs = []
                for d in dimensions: 
                    runs.extend([d]*parser_phase3.options.bootstrapping_runs)
                matrices = bootstrap_matrices*parser_phase3.options.bootstrapping_runs
        else:
            runs = dimensions
            matrices = [confdistmatrix for i in runs]
        for d in dimensions:
            if d > confdistmatrix.size:
                logging.error("ERROR: The embedded space must have a number of dimensions inferior to the original space.")
                exit(1)
        
        # Choose algorithm and prepare options
        embedding_options = []
        if parser_phase3.options.spe_mode == 'vanilla':
            embedder = StochasticProximityEmbedding()
            for r in range(len(runs)):
                embedding_options += [(matrices[r], 
                                  parser_phase3.options.neighborhood_cutoff, 
                                  runs[r],
                                  parser_phase3.options.maxlam,
                                  parser_phase3.options.minlam,
                                  parser_phase3.options.ncycle,
                                  parser_phase3.options.nstep,
                                  parser_phase3.options.stressfreq)]

        if parser_phase3.options.spe_mode == 'rn':
            embedder = RandomNeighborhoodStochasticProximityEmbedding()
            for r in range(len(runs)):
                embedding_options += [(matrices[r],
                                  parser_phase3.options.neighborhood_cutoff,
                                  parser_phase3.options.kn,
                                  runs[r],
                                  parser_phase3.options.maxlam,
                                  parser_phase3.options.minlam,
                                  parser_phase3.options.ncycle,
                                  parser_phase3.options.stressfreq)]

        if parser_phase3.options.spe_mode == 'knn':
            embedder = kNNStochasticProximityEmbedding()
            for r in range(len(runs)):
                embedding_options += [(matrices[r],
                                  parser_phase3.options.kn,
                                  runs[r],
                                  parser_phase3.options.maxlam,
                                  parser_phase3.options.minlam,
                                  parser_phase3.options.ncycle,
                                  parser_phase3.options.nstep,
                                  parser_phase3.options.stressfreq)]

        pc = ParallelCalculation(parser_phase3.options.coresn, embedder, embedding_options)
        
        # Run parallel calculation
        results = pc.run()
        sleep(1)

        embedded_spaces_perdim = {}
        stresses_perdim = {}

        # Sort out obtained spaces and their residual stress values

        if parser_phase3.options.estimate_error: # if bootstrap
            if parser_phase3.options.error_mode == "bootstrapping":
                k = 0
                for ndim in dimensions:
                    values = []
                    for i in range(parser_phase3.options.bootstrapping_runs):

                        header = "# ==== Number of dimensions: %d ==="%ndim
                        values.append(numpy.zeros((out_matrix_eln,out_matrix_eln)))

                        embedded_stress  = results[k][1][0]
                        embedded_space = results[k][1][1]
                        
                        kdes, resamples, embedded_ensembles = gen_kde_pdfs(embedded_space, ensemble_assignment, parser_phase3.options.nensembles, nsamples = parser_phase3.options.samples)

                        for pair in pairs_indeces:
                            this_value = dimred_ensemble_similarity(kdes[pair[0]], resamples[pair[0]], kdes[pair[1]],resamples[pair[1]])
                            values[-1][pair[0],pair[1]] = this_value
                            values[-1][pair[1],pair[0]] = this_value
                        
                        k += 1
                        outs = numpy.array(values)
                        avgs = numpy.average(outs, axis=0)
                        stds = numpy.std(outs, axis=0)
                        
                    print "== number of dimensions: %d ==" % ndim
                    print "averages:"
                    print_square_array(avgs)
                    print "standard deviations:"
                    print_square_array(stds)
                    print "values (ens.A-ens.B value_run_1 value_run_2 ... value_run_N):"
                    for i,j in pairs_indeces:
                        print "%d-%d\t" % (i+1,j+1),
                        print " ".join(["%.3f"%k for k in outs[:,i,j]])
                exit(0)

        for i in range(len(dimensions)):
            stresses_perdim[dimensions[i]] = []
            embedded_spaces_perdim[dimensions[i]] = []
            for j in range(1):
                stresses_perdim[dimensions[i]].append(results[j*len(dimensions)+i][1][0])
                embedded_spaces_perdim[dimensions[i]].append(results[j*len(dimensions)+i][1][1])

        for ndim in dimensions:

            embedded_spaces = embedded_spaces_perdim[ndim]
            embedded_stresses = stresses_perdim[ndim]

            embedded_stress = embedded_stresses[numpy.argmin(embedded_stresses)]
            embedded_space  = embedded_spaces[numpy.argmin(embedded_stresses)]

            kdes, resamples, embedded_ensembles = gen_kde_pdfs(embedded_space, ensemble_assignment, parser_phase3.options.nensembles, nsamples = parser_phase3.options.samples)

        # For every chosen dimension value:
            if parser_phase3.options.evaluate_convergence:
                fname = str(parser_phase3.options.outfiles)+"_convergence_dimred_%ddimensions" %ndim
                header = "=== convergence dimred, dimension %d: " % ndim
                #if parser_phase3.options.convergence_mode == 'half-half':
                #    fname+="_half-half.dat"
                #    header += "half-half ==="
                #    if parser_phase3.options.outfiles != None:
                #        fhandler = open(fname,'a')
                #    else:
                #        fhandler = None

                #    write_output_line(0,rawline=header, handler=fhandler)
                #    write_output_line(0, rawline="half-half convergence: %.3f" %
                #                                        dimred_ensemble_similarity(kdes[0], resamples[0], kdes[1],resamples[1]), fhandler=fhandler)

                #elif parser_phase3.options.convergence_mode == 'sliding-window':
                #    print "sliding window ==="
                #    for j in range(len(ensembles)-1):
                #        print "%.3f" % dimred_ensemble_similarity(kdes[j], resamples[j], kdes[j+1],resamples[j+1])
                #elif parser_phase3.options.convergence_mode == 'fixed-window':
                #    print "fixed window ==="
                #    for j in range(1,len(ensembles)):
                #        print "%.3f" % dimred_ensemble_similarity(kdes[0], resamples[0], kdes[j],resamples[j])
                #elif parser_phase3.options.convergence_mode == "increasing-half":
                #    fname += "_increasing-half.dat"
                #    header += "increasing half ==="
                #    if parser_phase3.options.outfiles != None:
                #        fhandler = open(fname,'a')
                #    else:
                #        fhandler = None

                #    kdes, resamples, embedded_ensembles = cumulative_gen_kde_pdfs(embedded_space, ensemble_assignment, parser_phase3.options.nensembles, nsamples = parser_phase3.options.samples, ens_id_max=len(ensembles))
                #    ref_kdes, ref_resamples, ref_embedded_ensembles = cumulative_gen_kde_pdfs(embedded_space, ensemble_assignment, parser_phase3.options.nensembles, nsamples = parser_phase3.options.samples, ens_id_max=len(ensembles), ens_id_min=len(ensembles))

                #    write_output_line(0,rawline=header, fhandler=fhandler)

                #    for j in range(0,len(ensembles)-1):
                #        write_output_line(dimred_ensemble_similarity(ref_kdes[0], 
                #                                                    ref_resamples[0], 
                #                                                    kdes[j], 
                #                                                    resamples[j]),
                #        fhandler = fhandler,
                #        number = j+1)

                if parser_phase3.options.convergence_mode=="increasing-window":
                    fname += "_increasing-window.dat"
                    header += "increasing window ==="
                    if parser_phase3.options.outfiles != None:
                        fhandler = open(fname,'a')
                    else:
                        fhandler = None

                    kdes, resamples, embedded_ensembles = cumulative_gen_kde_pdfs(embedded_space, ensemble_assignment, parser_phase3.options.nensembles-1, nsamples = parser_phase3.options.samples)
                    
                    write_output_line(0, rawline=header, fhandler=fhandler)
                    for j in range(0,len(ensembles)):
                        write_output_line(dimred_ensemble_similarity(kdes[-1], 
                                                                    resamples[-1], 
                                                                    kdes[j], 
                                                                    resamples[j]),
                        fhandler = fhandler,
                        number = j+1)
            else:

                                 
                for pair in pairs_indeces:
                    values[pair[0],pair[1]] = dimred_ensemble_similarity(kdes[pair[0]], resamples[pair[0]], kdes[pair[1]],resamples[pair[1]])
            
                header = "# ==== Number of dimensions: %d ==="%ndim
                write_output(values, header=header, base_fname=parser_phase3.options.outfiles, suffix="dimred-%ddimensions"%ndim)

            if parser_phase3.options.details:
                kwds = {}
                kwds["stress"] = numpy.array([embedded_stress])
                for en,e in enumerate(embedded_ensembles):
                    kwds[("ensemble%d"%en)] = e
                numpy.savez("%s_%d_dimensions" % (parser_phase3.options.details, ndim), **kwds)

        logging.info("Calculation complete.")
        exit(0)
