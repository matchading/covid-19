import math
import matplotlib.pyplot as plt
import numpy as np
import os
import pandas as pd
from pathlib import Path
import pickle
import platform
import pystan
import re
import sys


def get_data(roi, data_path='data'):
    path = Path(data_path) / ("covidtimeseries_%s.csv" % roi)
    assert path.is_file(), "No file found at %s" % (path.resolve())
    df = pd.read_csv(path).set_index('dates2')
    #df.index = pd.to_datetime(df.index)
    df = df[[x for x in df if 'Unnamed' not in x]]
    df.index.name = 'date'
    return df
    
def load_or_compile_stan_model(stan_name, force_recompile=False):
    stan_raw = '%s.stan' % stan_name
    stan_compiled = '%s_%s_%s.stanc' % (stan_name, platform.platform(), platform.python_version())
    stan_raw_last_mod_t = os.path.getmtime(stan_raw) 
    try:
        stan_compiled_last_mod_t = os.path.getmtime(stan_compiled) 
    except FileNotFoundError:
        stan_compiled_last_mod_t = 0
    if force_recompile or (stan_compiled_last_mod_t < stan_raw_last_mod_t):
        sm = pystan.StanModel(file=stan_raw)#, verbose=True)
        with open(stan_compiled, 'wb') as f:
            pickle.dump(sm, f)
    else:
        print("Loading %s from cache..." % stan_name)
        with open(stan_compiled, 'rb') as f:
            sm = pickle.load(f)
    return sm


def get_data_prefix():
    return 'covidtimeseries'


def get_ending(fit_format):
    if fit_format in [0]:
        ending = '.csv'
    elif fit_format==1:
        ending = '.pkl'
    else:
        raise Exception("No such fit format: %s" % fit_format)
    return ending


def list_rois(path, prefix, extension):
    """List all of the ROIs for which there is data or fits
    with ending `ending` at the given path.
    Assumes there are no underscores until immediately before
    the region begins in each potential file name"""
    if isinstance(path, str):
        path = Path(path)
    rois = []
    for file in path.iterdir():
        file_name = str(file.name)
        if file_name.startswith(prefix) and file_name.endswith(extension):
            roi = file_name.replace(prefix, '').replace(extension, '').strip('.').strip('_')
            rois.append(roi)
    return rois


def load_fit(fit_path, model_full_path, new_module_name=None):
    """This function will try to load a pickle file containing a Stan fit instance (and other things)
    If the compiled model is not found in memory, preventing the fit instance from being loaded, 
    it will trick Stan into thinking that the the model that is loaded is the model that belongs with that fit instance
    Then it will return samples"""
    try:
        with open(fit_path, "rb") as f:
            fit = pickle.load(f)
    except ModuleNotFoundError as e:
        matches = re.findall("No module named '([a-z0-9_]+)'", str(e))
        if matches:
            model = load_or_compile_stan_model(model_full_path)  # Load the saved, compiled model (or compile it)
            old_module_name = [x for x in sys.modules if 'stanfit4' in x][0]  # Get the name of the loaded model module in case we need it
            new_module_name = matches[0]
            sys.modules[new_module_name] = sys.modules[old_module_name]
            fit = load_fit(fit_path, old_module_name, new_module_name)
        else:
            raise ModuleNotFoundError("Module not found message did not parse correctly")
    else:
        fit = fit['fit']
    return fit


def extract_samples(fits_path, models_path, model_name, roi, fit_format):
    """Extract samples from the fit into a dataframe"""
    if fit_format in [0]:
        # Load the format that is just samples in a .csv file
        fit_path = Path(fits_path) / ("%s_%s.csv" % (model_name, roi))
        samples = pd.read_csv(fit_path)
    elif fit_format==1:
        # Load the format that is a pickle fit containing a Stan fit instance and some other things
        fit_path = Path(fits_path) / ("%s_%s.pkl" % (model_name, roi))
        model_full_path = Path(models_path) / model_name
        fit = load_fit(fit_path, model_full_path)
        samples = fit.to_dataframe()
    return samples


def make_table(roi, samples, params, quantiles=[0.05, 0.25, 0.5, 0.75, 0.95], chain=None):
    new_params = []
    for param in params:
        found = False
        if param in samples:
            new_params.append(param)
            found = True
        else:  # If not found in pure form
            for i in range(1000, 0, -1):  # Search for it in series from latest to earliest
                candidate = '%s[%d]' % (param, i)
                if candidate in samples:
                    new_params.append(candidate)  # Pick the latest one found
                    found = True
                    break  # And move on
        if not found:
            print("No param like %s is in the samples dataframe" % param)
    
    dfs = []
    cols = [col for col in samples if col in new_params]
    if chain:
        samples = samples[samples['chain']==chain]
    samples = samples[cols]
    q = samples.quantile(quantiles)#.reset_index(drop=True)
    dfs.append(q)
    df = pd.concat(dfs)
    df.columns = [x.split('[')[0] for x in df.columns]  # Drop the index
    df.index = pd.MultiIndex.from_product(([roi], df.index), names=['roi', 'quantile'])
    return df


def get_day_labels(data, days, t0):
    days, day_labels = zip(*enumerate(data.index[t0:]))
    day_labels = ['%s (%d)' % (day_labels[i][:-3], days[i]) for i in range(len(days))]
    return day_labels


def get_timing(roi, data_path):
    data = get_data(roi, data_path) # Load the data
    t0date = data[data["new_cases"]>=1].index[0]
    t0 = data.index.get_loc(t0date)
    try:
        dfm = pd.read_csv(Path(data_path) / 'mitigationprior.csv').set_index('region')
        tmdate = dfm.loc[roi, 'date']
        tm = data.index.get_loc(tmdate)
    except:
        print("No mitigation data found; falling back to default value")
        tm = t0 + 10
        tmdate = data.index[t0]
    
    print(t0, t0date, tm, tmdate)
    print("t0 = %s (day %d)" % (t0date, t0))
    print("tm = %s (day %d)" % (tmdate, tm))
    return t0, tm


def plot_data_and_fits(data_path, roi, samples, t0, tm, chains=None):
    data = get_data(roi, data_path)
    
    if chains is None:
        chains = samples['chain'].unique()
    
    fig, ax = plt.subplots(3, 2, figsize=(15, 10))
    days = range(data.shape[0])
    days_found = [day for day in days if 'lambda[%d,0]' % (day-t0) in samples]
    days_missing = set(days).difference(days_found)
    print("Empirical data for days %s is available but fit data for these days is missing" % days_missing)
    #day_labels = get_day_labels(data, days, t0)
    estimates = {}
    chain_samples = samples[samples['chain'].isin(chains)]
    
    for i, kind in enumerate(['cases', 'recover', 'deaths']):
        estimates[kind] = [chain_samples['lambda[%d,%d]' % (day-t0, i)].mean() for day in days_found]
        colors = 'bgr'
        cum = data["cum_%s" % kind]
        xticks, xlabels = zip(*[(i, x[:-3]) for i, x in enumerate(cum.index) if i%2==0])
        
        xlabels = [x[:-3] for i, x in enumerate(cum.index) if i%2==0]
        ax[i, 0].set_title('Cumulative %s' % kind)
        ax[i, 0].plot(cum, 'bo', color=colors[i], label=kind)
        ax[i, 0].axvline(t0, color='k', linestyle="dashed", label='t0')
        ax[i, 0].axvline(tm, color='purple', linestyle="dashed", label='mitigate')
        ax[i, 0].set_xticks(xticks)
        #ax[i, 0].get_xticklabels()
        ax[i, 0].set_xticklabels(xlabels, rotation=80, fontsize=8)
        ax[i, 0].legend()

        new = data["new_%s" % kind]
        ax[i, 1].set_title('Daily %s' % kind)
        ax[i, 1].plot(new, 'bo', color=colors[i], label=kind)
        ax[i, 1].axvline(t0, color='k', linestyle="dashed", label='t0')
        ax[i, 1].axvline(tm, color='purple', linestyle="dashed", label='mitigate')
        ax[i, 1].set_xticks(xticks)
        ax[i, 1].set_xticklabels(xlabels, rotation=80, fontsize=8)
        if kind in estimates:
            ax[i, 1].plot(days_found, estimates[kind], label=r'$\hat{%s}$'% kind, linewidth=2, alpha=0.5, color=colors[i])
    ax[i, 1].legend()

    plt.tight_layout()
    fig.suptitle(roi, y=1.02)
    

def make_histograms(samples, hist_params, cols=4, size=3):
    cols = min(len(hist_params), cols)
    rows = math.ceil(len(hist_params)/cols)
    fig, axes = plt.subplots(rows, cols, squeeze=False, figsize=(size*cols, size*rows))
    chains = samples['chain'].unique()
    for i, param in enumerate(hist_params):
        options = {}
        ax = axes.flat[i]
        for chain in chains:
            if ':' in param:
                param, options = param.split(':')
                options = eval("dict(%s)" % options)
            chain_samples = samples[samples['chain']==chain][param]
            if options.get('log', False):
                chain_samples = np.log(chain_samples)
            low, high = chain_samples.quantile([0.01, 0.99])
            ax.hist(chain_samples, alpha=0.5, bins=np.linspace(low, high, 25), label='Chain %d' % chain)
            if options.get('log', False):
                ax.set_xticks(np.linspace(chain_samples.min(), chain_samples.max(), 5))
                ax.set_xticklabels(['%.2g' % np.exp(x) for x in ax.get_xticks()])
            ax.set_title(param)
        plt.legend()
    plt.tight_layout()
    
    
def make_lineplots(samples, time_params, rows=4, cols=4, size=4):
    cols = min(len(time_params), cols)
    rows = math.ceil(len(time_params)/cols)
    fig, axes = plt.subplots(rows, cols, squeeze=False, figsize=(size*cols, size*rows))
    chains = samples['chain'].unique()
    colors = 'rgbk'
    for i, param in enumerate(time_params):
        options = {}
        ax = axes.flat[i]
        for chain in chains:
            cols = [col for col in samples if param in col]
            chain_samples = samples[samples['chain']==chain][cols]
            quantiles = chain_samples.quantile([0.05, 0.5, 0.95]).T.reset_index(drop=True)
            ax.plot(quantiles.index, quantiles[0.5], label=('Chain %d' % chain), color=colors[chain])
            ax.fill_between(quantiles.index, quantiles[0.05], quantiles[0.95], alpha=0.2, color=colors[chain])
        ax.legend()
        ax.set_title(param)
        ax.set_xlabel('Days')
    plt.tight_layout()
    
    
### Not working yet
def josh_likelihoods(samples):
    log_likelihoods = samples['ll_']    
    n = samples.shape[-1]
    likelihoods = np.exp(log_likelihoods)   # Result is S x N
    lppd = np.sum(np.log(np.mean(likelihoods, axis=0)))
    pwaic = np.sum(np.var(log_likelihoods, axis=0))
    elpdi = np.log(np.mean(likelihoods, axis=0)) - np.var(log_likelihoods, axis=0)
    se = 2*np.sqrt(n*np.var(elpdi))
    return {'waic': 2*(-lppd + pwaic), 'se': se}
