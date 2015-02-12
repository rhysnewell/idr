#Av: I wasn't able to install the idr package. This may have
#been becuase the paths set up by my preexisting Anaconda install
#conflicted with the paths required by python3 (which was not
#pre-installed on my mac). The specific error I got pertained
#to a numpy header that couldn't be found (even though numpy
#had been installed for python3 on my mac).
import os, sys

import gzip, io

import math

import numpy

from scipy.stats.stats import rankdata

from collections import namedtuple, defaultdict, OrderedDict
from itertools import chain

def mean(items):
    items = list(items)
    return sum(items)/float(len(items))

import idr

import idr.optimization
from idr.optimization import estimate_model_params, old_estimator
from idr.utility import calc_post_membership_prbs, compute_pseudo_values

#This method is used to create enums to simply the code.
def enum(**enums):
    toReturn = type('Enum', (), enums);
    toReturn.vals = enums.values();
    return toReturn;

Peak = namedtuple('Peak', ['chrm', 'strand', 'start', 'stop', 'signal'])
PEAK_INDICES = enum(chrm=0, strand=5, start=1, stop=2);
INPUT_FILE_TYPE = enum(narrowPeak = 'narrowPeak'
                        ,broadPeak = 'broadPeak'
                        ,bed='bed');
PEAK_MERGE_METHODS = enum(sum='sum', avg='avg', min='min', max='max');
peak_merge_fn_lookup = {
        PEAK_MERGE_METHODS.sum: sum, PEAK_MERGE_METHODS.avg: mean
        , PEAK_MERGE_METHODS.min: min, PEAK_MERGE_METHODS.max: max
}


def load_bed(fp, signal_index):
    """
    returns: grpd_peaks which is a dictionary indexed by
    (chromosome, strand) which contains a list of all
    the Peak objects for that chromsome and strand.
    """
    grpd_peaks = defaultdict(list)
    for line in fp:
        if line.startswith("#"): continue
        if line.startswith("track"): continue
        data = line.split()
        signal = float(data[signal_index])
        if signal < 0: 
            raise ValueError("Invalid Signal Value: {:e}".format(signal))
        
        #Av: You were using straight int constants for the indices below.
        #I swapped in the enum approach for readability/maintainability.
        peak = Peak(data[PEAK_INDICES.chrom], data[PEAK_INDICES.strand], 
                    int(float(data[PEAK_INDICES.start]))
                    , int(float(data[PEAK_INDICES.stop])), 
                    signal )
        #Av: re. int(float(..)) for start/end - I guess this is because
        #sometimes some programs can unintentionally format the chrom
        #start/end as floats rather than integers? Do you want to
        #put in a robustness check to make sure that those positions
        #are actually integers?
        #If so, you can keep the lines below:
        for (idxOfInteger, valName) in [(PEAK_INDICES.start, 'start')
                                        ,(PEAK_INDICES.stop, 'stop')]:
            if int(float(data[idxOfInteger])) != float(data[idxOfInteger]):
                raise ValueError(
                    "Expected "+valName+" at index "+str(idxOfInteger)
                    +" to be an integer but got "+str(data[idxOfInteger]));
        grpd_peaks[(peak.chrm, peak.strand)].append(peak)
    return grpd_peaks

def merge_peaks_in_contig(s1_peaks, s2_peaks, pk_agg_fn, oracle_pks=None,
                          use_nonoverlapping_peaks=False):
    """Merge peaks in a single contig/strand.
        pk_agg_fn: the aggregate function for peak merging
        oracle_pks: (optional) the peaks specified in peak-list. 
    returns: The merged peaks. 
    """
    # merge and sort all peaks, keeping track of which sample they originated in
    if oracle_pks == None: oracle_pks_iter = []
    else: oracle_pks_iter = oracle_pks
    all_intervals = sorted(chain(
            ((pk.start,pk.stop,pk.signal,1) for pk in s1_peaks),
            ((pk.start,pk.stop,pk.signal,2) for pk in s2_peaks),
            ((pk.start,pk.stop,pk.signal,0) for pk in oracle_pks_iter)))
    
    # grp overlapping intervals. Since they're already sorted, all we need
    # to do is check if the current interval overlaps the previous interval
    # (importantly, they are sorted by the start index, then by the stop
    # index).
    grpd_intervals = [[],]
    curr_start, curr_stop = all_intervals[0][:2]
    for x in all_intervals:
        # due to the sorting, we are guaranteed that x[1] > curr_start.
        # Note that the condition below successfully includes the
        # first peak.
        if x[0] < curr_stop:
            curr_stop = max(x[1], curr_stop)
            grpd_intervals[-1].append(x)
        else:
            curr_start, curr_stop = x[:2]
            grpd_intervals.append([x,])

    # build the unified peak list, setting the score to 
    # zero if it doesn't exist in both replicates
    merged_pks = []
    for intervals in grpd_intervals:
        # grp peaks by their source, and calculate the merged
        # peak boundaries
        grpd_peaks = OrderedDict(((1, []), (2, [])))
        #Av: changed 1e9 to 1e10, because I think some
        #organisms have much larger chromosomes than humans
        pk_start, pk_stop = 1e10, -1
        for rep_start, rep_stop, signal, sample_id in intervals:
            # if we've provided a unified peak set, ignore any intervals that 
            # don't contain it for the purposes of generating the merged list
            # The way the condition below works is that if a particular set
            # of grouped peaks has no oracle_peaks, but oracle speaks was
            # specified, pk_stop will end up being -1 - and that will be
            # used later in a filtering condition.
            if oracle_pks == None or sample_id == 0:
                pk_start = min(rep_start, pk_start)
                pk_stop = max(rep_stop, pk_stop)
            # if this is an actual sample (ie not a merged peaks)
            # (sample_id is 0 for oracle_peaks)
            if sample_id > 0:
                grpd_peaks[sample_id].append(
                    (rep_start, rep_stop, signal, sample_id))
        
        # if there are no identified peaks, continue (this can happen if 
        # we have a merged peak list (oracle peaks)
        # but no merged peaks overlap sample peaks)
        if pk_stop == -1: continue
        
        # skip regions that dont have a peak in all replicates
        if not use_nonoverlapping_peaks:
            if any(0 == len(peaks) for peaks in grpd_peaks.values()):
                continue
       
        # 2 is the index for the peak_signal.
        #Av: The use of the raw constant '2' over here as the index
        # for the signal makes me wish 'Peaks' were an object, not
        #just a tuple, but I recognise that adds overhead.
        #I still wish there were a way to couple the index
        #here to the *actual* place where the signal is stored.
        s1, s2 = (pk_agg_fn(pk[2] for pk in pks)
                  for pks in grpd_peaks.values())
                  
        merged_pk = (pk_start, pk_stop, s1, s2, grpd_peaks)
        merged_pks.append(merged_pk)

    return merged_pks

def merge_peaks(s1_peaks, s2_peaks, pk_agg_fn, oracle_pks=None, 
                use_nonoverlapping_peaks=False):
    """Merge peaks over all contig/strands
        pk_agg_fn: the aggregate function for peak merging
        oracle_pks: (optional) the set of peaks to use  
    """
    # if we have reference peaks, use its contigs: otherwise use
    # the union of the replicates contigs
    if oracle_pks != None:
        contigs = sorted(oracle_pks.keys())
    else:
        contigs = sorted(set(chain(s1_peaks.keys(), s2_peaks.keys())))
    
    merged_peaks = []
    for key in contigs:
        # check to see if we've been provided a peak list and, if so, 
        # pass it down. If not, set the oracle peaks to None so that 
        # the callee knows not to use them
        if oracle_pks != None: contig_oracle_pks = oracle_pks[key]
        else: contig_oracle_pks = None
        
        # since s*_peaks are default dicts, it will never raise a key error, 
        # but instead return an empty list which is what we want
        # Av: (BUG) use_non_overlapping_peaks was being set to False; I took
        # that out, now it uses the passed in value.
        merged_peaks.extend(
            key + pk for pk in merge_peaks_in_contig(
                s1_peaks[key], s2_peaks[key], pk_agg_fn, contig_oracle_pks, 
                use_nonoverlapping_peaks))
   
    # 4 and 5 are the indexes for the signal from rep 1 and rep2,
    # respectively.
    #Av: Augh more raw constants. This is again why I wish Peak were an
    #object.
    #Av: also...if the aggregate function is 'mean', then the result
    #here is NOT the same as taking the mean across all peaks in all
    #replicates - because this gives equal weight to each replicate...
    #is this deliberate? 
    merged_peaks.sort(key=lambda x:pk_agg_fn((x[4],x[5])), reverse=True)
    return merged_peaks

def build_rank_vectors(merged_peaks):
    # allocate memory for the ranks vector
    s1 = numpy.zeros(len(merged_peaks))
    s2 = numpy.zeros(len(merged_peaks))
    # add the signal
    for i, x in enumerate(merged_peaks):
        #x[4] is the signal from rep1, x[5] from rep2.
        #Av: Aaah! Raw Indexes!
        s1[i], s2[i] = x[4], x[5]

    #ranks the signal by its strength and breaks ties randomly
    #Av: I think the argsort at the end is redundant. lexsort appears
    #to return indices.
    rank1 = numpy.lexsort((numpy.random.random(len(s1)), s1)).argsort()
    rank2 = numpy.lexsort((numpy.random.random(len(s2)), s2)).argsort()
    
    return ( numpy.array(rank1, dtype=numpy.int), 
             numpy.array(rank2, dtype=numpy.int) )

def build_idr_output_line(
    contig, strand, signals, merged_peak, IDR, localIDR):
    rv = [contig,]
    for signal, key in zip(signals, (1,2)):
        if len(merged_peak[key]) == 0: 
            rv.extend(("-1", "-1"))
        else:
            rv.append( "%i" % min(x[0] for x in merged_peak[key]))
            rv.append( "%i" % max(x[1] for x in merged_peak[key]))
        rv.append( "%.5f" % signal )
    
    rv.append("%.5f" % IDR)
    rv.append("%.5f" % localIDR)
    rv.append(strand)
        
    return "\t".join(rv)

def calc_IDR(theta, r1, r2):
    """
    idr <- 1 - e.z
    o <- order(idr)
    idr.o <- idr[o]
    idr.rank <- rank(idr.o, ties.method = "max")
    top.mean <- function(index, x) {
        mean(x[1:index])
    }
    IDR.o <- sapply(idr.rank, top.mean, idr.o)
    IDR <- idr
    IDR[o] <- IDR.o
    """
    mu, sigma, rho, p = theta
    z1 = compute_pseudo_values(r1, mu, sigma, p, EPS=1e-12)
    z2 = compute_pseudo_values(r2, mu, sigma, p, EPS=1e-12)
    localIDR = 1-calc_post_membership_prbs(numpy.array(theta), z1, z2)
    if idr.FILTER_PEAKS_BELOW_NOISE_MEAN:
        localIDR[z1 + z2 < 0] = 1 
    local_idr_order = localIDR.argsort()
    ordered_local_idr = localIDR[local_idr_order]
    ordered_local_idr_ranks = rankdata( ordered_local_idr, method='max' )
    IDR = []
    for i, rank in enumerate(ordered_local_idr_ranks):
        IDR.append(ordered_local_idr[:rank].mean())
    IDR = numpy.array(IDR)[local_idr_order.argsort()]

    return localIDR, IDR

def fit_model_and_calc_idr(r1, r2, 
                           starting_point=None,
                           max_iter=idr.MAX_ITER_DEFAULT, 
                           convergence_eps=idr.CONVERGENCE_EPS_DEFAULT, 
                           fix_mu=False, fix_sigma=False ):
    # in theory we would try to find good starting point here,
    # but for now just set it to somethign reasonable
    if starting_point == None:
        starting_point = (DEFAULT_MU, DEFAULT_SIGMA, 
                          DEFAULT_RHO, DEFAULT_MIX_PARAM)
    
    idr.log("Initial parameter values: [%s]" % " ".join(
            "%.2f" % x for x in starting_point))
    
    # fit the model parameters    
    idr.log("Fitting the model parameters", 'VERBOSE');
    if idr.PROFILE:
            import cProfile
            cProfile.runctx("""theta, loss = estimate_model_params(
                                    r1,r2,
                                    starting_point, 
                                    max_iter=max_iter, 
                                    convergence_eps=convergence_eps,
                                    fix_mu=fix_mu, fix_sigma=fix_sigma)
                                   """, 
                            {'estimate_model_params': estimate_model_params}, 
                            {'r1':r1, 'r2':r2, 
                             'starting_point': starting_point,
                             'max_iter': max_iter, 
                             'convergence_eps': convergence_eps,
                             'fix_mu': fix_mu, 'fix_sigma': fix_sigma} )
            assert False
    theta, loss = estimate_model_params(
        r1, r2,
        starting_point, 
        max_iter=max_iter, 
        convergence_eps=convergence_eps,
        fix_mu=fix_mu, fix_sigma=fix_sigma)
    
    idr.log("Finished running IDR on the datasets", 'VERBOSE')
    idr.log("Final parameter values: [%s]"%" ".join("%.2f" % x for x in theta))
    
    # calculate the global IDR
    localIDRs, IDRs = calc_IDR(numpy.array(theta), r1, r2)

    return localIDRs, IDRs

def write_results_to_file(merged_peaks, output_file, 
                          max_allowed_idr=idr.DEFAULT_IDR_THRESH,
                          soft_max_allowed_idr=idr.DEFAULT_SOFT_IDR_THRESH,
                          localIDRs=None, IDRs=None):
    # write out the result
    idr.log("Writing results to file", "VERBOSE");
    
    if localIDRs == None or IDRs == None:
        assert IDRs == None
        assert localIDRs == None
        localIDRs = numpy.ones(len(merged_peaks))
        IDRs = numpy.ones(len(merged_peaks))

    
    num_peaks_passing_hard_thresh = 0
    num_peaks_passing_soft_thresh = 0
    for localIDR, IDR, merged_peak in zip(
            localIDRs, IDRs, merged_peaks):
        # skip peaks with global idr values below the threshold
        if max_allowed_idr != None and IDR > max_allowed_idr: 
            continue
        num_peaks_passing_hard_thresh += 1
        if IDR <= soft_max_allowed_idr:
            num_peaks_passing_soft_thresh += 1
        opline = build_idr_output_line(
            merged_peak[0], merged_peak[1], 
            merged_peak[4:6], 
            merged_peak[6], IDR, localIDR )
        print( opline, file=output_file )

    idr.log(
        "Number of reported peaks - {}/{} ({:.1f}%)\n".format(
            num_peaks_passing_hard_thresh, len(merged_peaks),
            100*float(num_peaks_passing_hard_thresh)/len(merged_peaks))
    )
    
    idr.log(
        "Number of peaks passing IDR cutoff of {} - {}/{} ({:.1f}%)\n".format(
            args.soft_idr_threshold, 
            num_peaks_passing_soft_thresh, len(merged_peaks),
            100*float(num_peaks_passing_thresh)/len(merged_peaks))
    )
    
    return

def parse_args():
    import argparse

    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawTextHelpFormatter,
        description="""
Program: IDR (Irreproducible Discovery Rate)
Version: {PACKAGE_VERSION}
Contact: Nathan Boley <npboley@gmail.com>
         Nikhil R Podduturi <nikhilrp@stanford.edu>
""".format(PACKAGE_VERSION=idr.__version__))

    def PossiblyGzippedFile(fname):
        #Av: do they every end with 'gzip'?
        if fname.endswith(".gz"):
            return io.TextIOWrapper(gzip.open(fname, 'rb'))
        else:
            return open(fname, 'r')
    
    parser.add_argument( '--samples', '-s', type=PossiblyGzippedFile, nargs=2, 
                         required=True,
                         help='Files containing peaks and scores.')
    parser.add_argument( '--peak-list', '-p', type=PossiblyGzippedFile,
        help='If provided, all peaks will be taken from this file.')
    parser.add_argument( '--input-file-type', default=INPUT_FILE_TYPE.narrowPeak,
        choices=INPUT_FILE_TYPE.vals, 
        help='File type of --samples and --peak-list.')
    
    parser.add_argument( '--rank',
        help="Which column to use to rank peaks."\
            +"\t\nOptions: signal.value p.value q.value columnIndex"\
            +"\nDefaults:\n\tnarrowPeak/broadPeak: signal.value\n\tbed: score")
    
    default_ofname = "idrValues.txt"
    parser.add_argument( '--output-file', "-o", type=argparse.FileType("w"), 
                         default=open(default_ofname, "w"), 
        help='File to write output to.\nDefault: {}'.format(default_ofname))

    parser.add_argument( '--log-output-file', "-l", type=argparse.FileType("w"),
                         default=sys.stderr,
                         help='File to write output to. Default: stderr')
    
    parser.add_argument( '--idr-threshold', "-i", type=float, 
                         default=idr.DEFAULT_IDR_THRESH, 
        help="Only return peaks with a global idr threshold below this value."\
            +"\nDefault: report all peaks")
    parser.add_argument( '--soft-idr-threshold', type=float, default=None, 
        help="Report statistics for peaks with a global idr below this "\
            +"value but return all peaks.\nDefault: --idr if set else %.2f"
                         % idr.DEFAULT_SOFT_IDR_THRESH)

    parser.add_argument( '--plot', action='store_true', default=False,
                         help='Plot the results to [OUTPUTFILENAME].png')
        
    parser.add_argument( '--use-nonoverlapping-peaks', 
                         action="store_true", default=False,
        help='Use peaks without an overlapping match and set the value to 0.')
    
    parser.add_argument( '--peak-merge-method', 
                         choices=PEAK_MERGE_METHODS.vals, default=None,
        help="Which method to use for merging peaks.\n" \
              + "\tDefault: 'avg' for signal/score, 'min' for p/q-value.")

    parser.add_argument( '--initial-mu', type=float, default=idr.DEFAULT_MU,
        help="Initial value of mu. Default: %.2f" % idr.DEFAULT_MU)
    parser.add_argument( '--initial-sigma', type=float, 
                         default=idr.DEFAULT_SIGMA,
        help="Initial value of sigma. Default: %.2f" % idr.DEFAULT_SIGMA)
    parser.add_argument( '--initial-rho', type=float, default=idr.DEFAULT_RHO,
        help="Initial value of rho. Default: %.2f" % idr.DEFAULT_RHO)
    parser.add_argument( '--initial-mix-param', 
        type=float, default=idr.DEFAULT_MIX_PARAM,
        help="Initial value of the mixture params. Default: %.2f" \
                         % idr.DEFAULT_MIX_PARAM)

    parser.add_argument( '--fix-mu', action='store_true', 
        help="Fix mu to the starting point and do not let it vary.")    
    parser.add_argument( '--fix-sigma', action='store_true', 
        help="Fix sigma to the starting point and do not let it vary.")    
    
    parser.add_argument( '--max-iter', type=int, default=idr.MAX_ITER_DEFAULT, 
        help="The maximum number of optimization iterations. Default: %i" 
                         % idr.MAX_ITER_DEFAULT)
    parser.add_argument( '--convergence-eps', type=float, 
                         default=idr.CONVERGENCE_EPS_DEFAULT, 
        help="The maximum change in parameter value changes " \
             + "for convergence. Default: %.2e" % idr.CONVERGENCE_EPS_DEFAULT)
    
    parser.add_argument( '--only-merge-peaks', action='store_true', 
        help="Only return the merged peak list.")    
    
    parser.add_argument( '--verbose', action="store_true", default=False, 
                         help="Print out additional debug information")
    parser.add_argument( '--quiet', action="store_true", default=False, 
                         help="Don't print any status messages")

    args = parser.parse_args()

    idr.log_ofp = args.log_output_file
    
    if args.verbose: 
        idr.VERBOSE = True 

    global QUIET
    if args.quiet: 
        idr.QUIET = True 
        idr.VERBOSE = False

    if args.plot:
        try: 
            import matplotlib
            if args.soft_idr_threshold == None:
                if args.idr_threshold != None:
                    args.soft_idr_threshold = args.idr_threshold
                else:
                    args.soft_idr_threshold = idr.DEFAULT_SOFT_IDR_THRESH
        except ImportError:
            idr.log("WARNING: matplotlib does not appear to be installed and "\
                    +"is required for plotting - turning plotting off.", 
                    level="WARNING" )
            args.plot = False
    
    return args

def load_samples(args):
    # decide what aggregation function to use for peaks that need to be merged
    idr.log("Loading the peak files", 'VERBOSE')
    if args.input_file_type in [INPUT_FILE_TYPE.narrowPeak, INPUT_FILE_TYPE.broadPeak]:
        if args.rank == None: signal_type = 'signal.value'
        else: signal_type = args.rank
        (score_idx, signal_idx) = (4,6); #assigning to variables for readability
        try: 
            signal_index = {"score": score_idx, "signal.value": signal_idx, 
                            "p.value": 7, "q.value": 8}[signal_type]
        except KeyError:
            raise ValueError(
                "Unrecognized signal type for {} filetype: '{}'".format(
                    args.input_file_type, signal_type))

        if args.peak_merge_method != None:
            peak_merge_fn = peak_merge_fn_lookup[args.peak_merge_method]
            #Av: adding the check below in case you ever decide
            #to add more peak merge methods.
            assert peak_merge_fn is not None;
        #N.B. to developer: if you change these defaults, please
        #also change the help message in argparse.
        elif signal_index in (score_idx, signal_idx):
            peak_merge_fn = sum
        else: #p.value, q.value
            peak_merge_fn = mean
    elif args.input_file_type in [INPUT_FILE_TYPE.bed, ]:
        score_idx = 4;
        if args.rank != None: 
            if args.rank == 'score':
                signal_index = score_idx;
            else:
                try: signal_index = int(args.rank)
                except ValueError:
                    raise ValueError("For bed files --signal-type must either "\
                                     +"be set to score or an index specifying "\
                                     +"the column to use.")
        #Av: (BUG) it doesn't look like you had a case for when rank
        #was none, and since rank was an optional argument,
        #I think the clause below is necessary.
        else:
            signal_index = score_idx;
        if args.peak_merge_method != None:
            peak_merge_fn = peak_merge_fn_lookup[args.peak_merge_method]
        #N.B. to developer: if you change these defaults, please
        #also change the help message in argparse.
        else:
            peak_merge_fn = sum
    else:
        raise ValueError( "Unrecognized file type: '{}'".format(
            args.input_file_type))
    
    #Av: this code was repeated twice in each if branch above. I
    #moved it out.    
    #f1 and f2 are dictionaries with the index being (chrom, strand)
    #and the values being the list of peaks on that (chrom, strand)
    f1, f2 = [load_bed(fp, signal_index) for fp in args.samples]
    oracle_pks =  (
        load_bed(args.peak_list, signal_index) 
        if args.peak_list != None else None)
    
    # build a unified peak set
    idr.log("Merging peaks", 'VERBOSE')
    merged_peaks = merge_peaks(f1, f2, peak_merge_fn, 
                               oracle_pks, args.use_nonoverlapping_peaks)
    return merged_peaks

def main():
    args = parse_args()

    # load and merge peaks
    merged_peaks = load_samples(args)
    
    # build the ranks vector
    idr.log("Ranking peaks", 'VERBOSE')
    r1, r2 = build_rank_vectors(merged_peaks)
    
    if args.only_merge_peaks:
        localIDRs, IDRs = None, None
    else:
        if len(merged_peaks) < 20:
            error_msg = "Peak files must contain at least 20 peaks post-merge"
            error_msg += "\nHint: Merged peaks were written to the output file"
            write_results_to_file(
                merged_peaks, args.output_file )
            raise ValueError(error_msg)

        localIDRs, IDRs = fit_model_and_calc_idr(
            r1, r2, 
            starting_point=(
                args.initial_mu, args.initial_sigma, 
                args.initial_rho, args.initial_mix_param),
            max_iter=args.max_iter,
            convergence_eps=args.convergence_eps,
            fix_mu=args.fix_mu, fix_sigma=args.fix_sigma )    
        
        if args.plot:
            import matplotlib
            matplotlib.use('Agg')
            import matplotlib.pyplot
            
            colors = numpy.full(len(r1), 'k', dtype=str)
            colors[IDRs < args.plot_idr] = 'r'

            matplotlib.pyplot.axis([0, 1, 0, 1])
            matplotlib.pyplot.xlabel(args.a.name)
            matplotlib.pyplot.ylabel(args.b.name)
            matplotlib.pyplot.title("IDR Ranks - (red <= %.2f)" % args.plot_idr)
            matplotlib.pyplot.scatter((r1+1)/float(len(r1)+1), 
                                      (r2+1)/float(len(r2)+1), 
                                      c=colors,
                                      alpha=0.05)
            matplotlib.pyplot.savefig(args.output_file.name + ".png")
    
    num_peaks_passing_thresh = write_results_to_file(merged_peaks, 
                          args.output_file, 
                          max_allowed_idr=args.idr_threshold,
                          localIDRs=localIDRs, IDRs=IDRs)
    
    args.output_file.close()

if __name__ == '__main__':
    try:
        main()
    finally:
        if idr.log_ofp != sys.stderr: idr.log_ofp.close()
