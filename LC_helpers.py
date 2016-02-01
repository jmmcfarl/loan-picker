# -*- coding: utf-8 -*-
"""
Created on Mon Jan  4 16:05:51 2016

@author: james
"""
import numpy as np
import pandas as pd
import seaborn as sns

import matplotlib.colors as colors
import matplotlib.cm as cmx
import matplotlib.pyplot as plt
import matplotlib.colorbar as cbar
import re

#%%
def get_amortization_schedules(LD, term, calc_int = True):
    
    n_loans = len(LD)
    term_array = np.tile(np.arange(term+1),(n_loans,1)) #array of vectors going from 0 to term
    num_payments_array = LD['num_pymnts'][:,np.newaxis] * np.ones((n_loans,term+1)) #array of copies of the number of payments for each loan
    monthly_int = LD['int_rate'][:,np.newaxis]/12./100. #monthly interest rate for each loan
    
    #get amortization schedule
    prin_sched = ((1 + monthly_int) ** term_array - 1) / ((1 + monthly_int) ** term - 1)
    prin_sched = LD['funded_amnt'][:,np.newaxis]*(1 - prin_sched)

    #compute cumulative sum of payments actually received
    prin_sched_obs = prin_sched.copy() #over observed terms
    prin_sched_obs[num_payments_array <= term_array] = 0
   
    if not calc_int:
        return (prin_sched, prin_sched_obs)
    else: 
        int_sched = LD['installment'][:,np.newaxis] + np.diff(prin_sched,axis=1)    
        int_sched = np.concatenate((np.zeros((n_loans,1)),int_sched),axis=1) #include a month0, for loans that default before they make any payments
        return (prin_sched, prin_sched_obs, int_sched)
        

#%% calculate NARs (only works for observed data)
def get_NARs(LD, term, service_rate = 0.01):
    """compute NARs for all loans in dataframe LD. Uses precomputed hazard functions 
    to estimate expected NARs for immature loans.
        INPUTS: 
            LD: dataframe of loan data
            term: loan term (scalar) for the data in LD
            service_rate: percent of payments LC takes as service charge
        RETURNS: 
            tuple containing NAR and duration-weighted NAR (NAR, weighted_NAR)            
    """
      
    #net gain is total payment recieved less principal less collection recovery fees
    tot_gain = LD['total_pymnt'] - LD['total_rec_prncp'] - LD['collection_recovery_fee']
    
    #compute loss for charged off loans 
    tot_loss = LD['funded_amnt'] - LD['total_rec_prncp']
    tot_loss[LD['loan_status'] != 'Charged Off'] = 0 #set loss to 0 for loans that arent charged off
    
    #for loans where the principal is charged off and then 
    #later fully recovered. set the loan duration to be the loan term. This is a hack,
    #but we dont know when it's actually recovered, and this at least prevents
    #these loans from appearing like they provide gigantic returns
    post_CO_rec = (LD['loan_status'] == 'Charged Off') & (LD['recoveries'] > LD['funded_amnt'])
    LD.ix[post_CO_rec,'num_pymnts'] = LD.ix[post_CO_rec,'term']        
          
    (p_sched, p_sched_obs) = get_amortization_schedules(LD, term, calc_int = False)
    
    csum_prnc = np.sum(p_sched_obs,axis=1)
    
    
    #compute total service charge fee for each loan
    service_charge = service_rate * LD['total_pymnt']
    #NEED TO PUT THRESHOLD ON SERVICE CHARGE TO HANDLE EARLY REPAYMENT
    early_repay = (LD['total_pymnt'] == LD['funded_amnt']) & \
                    (LD['num_pymnts'] <= 12)
    max_sc = LD['num_pymnts'] * LD['installment'] * service_rate
    service_charge[early_repay] = max_sc[early_repay]
    
    #take interest made, less lossed principal less total service fees to get net gains
    net_gains = (tot_gain - tot_loss - service_charge)
    
    avg_duration = LD.ix[LD.is_observed,'num_pymnts'].mean() #avg duration of matured loans
    avg_monthly_return = net_gains/csum_prnc #avg monthly return weighted by outstanding prncp
    wavg_monthly_return = avg_monthly_return * LD['num_pymnts'] / avg_duration #weighted by loan duration
    
    NAR = (1 + avg_monthly_return) ** 12 - 1 
    weighted_NAR = (1 + wavg_monthly_return) ** 12 - 1
    return (NAR, weighted_NAR, avg_monthly_return)
    
#%%
def get_expected_NARs(LD, term, hazard_funs, service_rate = 0.01):
    """compute NARs for all loans in dataframe LD. Uses precomputed hazard functions 
    to estimate expected NARs given loan status for immature loans.
        INPUTS: 
            LD: dataframe of loan data
            term: loan term (scalar) for the data in LD
            hazard_funs: array of hazard functions for each loan grade
            service_rate: percent of payments LC takes as service charge
        RETURNS: 
            tuple containing exp_NAR and duration-weighted exp_NAR (exp_NAR, weighted_exp_NAR)            
    """
    
    #https://www.lendingclub.com/info/demand-and-credit-profile.action
    outcome_map = { #expected principal recovery given status, view this is prob of all princ being charged off
        'Current': 0,
        'Fully Paid': 0,
        'In Grace Period': 28,
        'Late (16-30 days)': 58,
        'Late (31-120 days)': 74,
        'Default': 89,
        'Charged Off': 100}

    (p_sched, p_sched_obs, int_sched) = get_amortization_schedules(LD, term)
    term_array = np.tile(np.arange(term+1),(len(LD),1)) #array of vectors going from 0 to term
    num_payments_array = LD['num_pymnts'][:,np.newaxis] * np.ones((len(LD),term+1)) #array of copies of the number of payments for each loan

    interest_paid = np.cumsum(int_sched, axis=1)
    cash_flows = interest_paid - term_array * LD['installment'][:,np.newaxis] * service_rate - p_sched
    monthly_returns = cash_flows / np.cumsum(p_sched, axis=1)

    #make array containing grade-conditional hazard fnxs for each loan             
    grades = np.sort(LD.grade.unique()) #set of unique loan grades
    grade_map = {grade: idx for idx,grade in enumerate(grades)} #dict mapping grade letters to index values in the hazard_funs arrays
    grade_index = LD['grade'].apply(lambda x: grade_map[x]).astype(int)
    def_probs = hazard_funs[grade_index.values,:]

    #actual_probs = hazard_array.copy()
    def_probs[num_payments_array > term_array] = 0 #prob of defaulting for made payments is 0
    
    def_probs[(LD.loan_status == 'Fully Paid').values,:] = 0 #set default probs for paid loans to 0
    
    #handle charged off loans
    CO_set = (LD.loan_status == 'Charged Off').values #set of charged off loans
    CO_probs = def_probs[CO_set,:] #get default probs for those loans
    CO_probs[num_payments_array[CO_set,:] == term_array[CO_set,:]] = 1.
    CO_probs[num_payments_array[CO_set,:] < term_array[CO_set,:]] = 0.
    def_probs[CO_set,:] = CO_probs
    
    #find loans that are 'in limbo'
    in_limbo_labels = ['In Grace Period','Late (16-30 days)', 'Late (31-120 days)','Default']
    in_limbo = LD.loan_status.isin(in_limbo_labels).values
    
    #conditional probability of being charged off 
    prob_CO = pd.to_numeric(LD['loan_status'].replace(outcome_map),errors='coerce')/100.
    
    #compute default probs for these loans separately
    dp_limbo = def_probs[in_limbo,:]
    #set default prob based on map at the current time
    dp_limbo[num_payments_array[in_limbo,:] == term_array[in_limbo,:]] = prob_CO[in_limbo] 
    
    #assume that if the loan doesnt end up getting written off then we return to normal default probs for remaining time
    #this means that we have to weight the contribution of these guys by the prob that you didnt default given delinquent status
    new_weight_mat = (1 - prob_CO[in_limbo, np.newaxis]) * dp_limbo    
    #adjust default probs for subsequent times
    later_time = num_payments_array[in_limbo,:] < term_array[in_limbo,:]
    dp_limbo[later_time] = new_weight_mat[later_time] 
    
    def_probs[in_limbo,:] = dp_limbo #store in-limbo values back in full array
    
    tot_default_prob = np.sum(def_probs, axis=1)
    pay_prob = 1 - tot_default_prob #total payment prob
    
    #expected returns is average monthly returns given default at each time, weighted by conditional probs of defaulting
    cond_monthly_returns = np.sum(def_probs * monthly_returns,axis=1) + pay_prob * monthly_returns[:,-1]
   
    #now weight by loan duration
    avg_duration = LD[LD.is_observed].num_pymnts.mean()
    dur_weights = (term_array + 1) / avg_duration   
    weight_cond_monthly_returns = np.sum(def_probs * monthly_returns * dur_weights,axis=1) + pay_prob * monthly_returns[:,-1]

    exp_num_pymnts = np.sum((term_array + 1) * def_probs, axis=1) + pay_prob*term #expected number of payments
    exp_NAR = (1 + cond_monthly_returns) ** 12 - 1
    weighted_exp_NAR = (1 + weight_cond_monthly_returns) ** 12 - 1
    
    return (exp_NAR, weighted_exp_NAR, cond_monthly_returns, tot_default_prob, exp_num_pymnts)
    
    
def extract_fips_coords(county_paths):
    '''Get the geographic coordinates (AU) of each county (fips) in the map'''
    coord_dict = {}
    re_path = re.compile(r'path d=\"(.*)\" id')
    re_ML = re.compile(r'[MLz]')
    for county in county_paths:
        path = re.findall(re_path,str(county))[0]
#        if path:
#            path = path[0]            
        coord_list = []
        numpair_set = re.split(re_ML,path)
        for numpair in numpair_set:
            if len(numpair) > 1:
                coord_list.append([float(el) for el in numpair.split(',')])
        avg_coords = np.mean(np.array(coord_list),axis=0)
        coord_dict[county['id']] = avg_coords
    coord_df = pd.DataFrame(coord_dict).transpose()
    coord_df.index.name = 'fips'
    coord_df.columns = ['x_coord','y_coord']
    return coord_df


def fill_missing_fips(ktree,map_coords,fips_data):
    '''Take a dataframe indexed by fips coordinates and fill in data for
    missing fips using nearest neighbor lookup '''    
    missing_fips = [fips for fips in map_coords.index.values 
                    if fips not in fips_data.index.values]
    new_data = {}
    for fips in missing_fips:
        knn_ds, knn_ids = ktree.query(map_coords.ix[fips].values,2)
        closest_fips = str(map_coords.index[knn_ids[1]])
        cnt = 3
        while closest_fips not in fips_data.index.values: #if nearest neighbor doesn't exist
            knn_ds, knn_ids = ktree.query(map_coords.ix[fips].values,cnt)
            closest_fips = str(map_coords.index[knn_ids[-1]])
            cnt += 1

        closest_data = fips_data.ix[closest_fips,'preds']
        new_data[fips] = closest_data

    new_df = pd.DataFrame({'preds':new_data})    
    new_df.index.name = 'fips'
    new_data = pd.concat([fips_data,new_df],axis=0)
    return new_data
 
 
def paint_map(data, soup_map, county_paths = None, fips_to_zip = None, 
              color = 'blue', get_cbar = True, agg_fun='mean'):
    '''paints the data onto an svg base map based on either zip3 or states'''
    
    #set base path style properties
    if (data.index.name == 'zip3') | (data.index.name == 'zip_code') | (data.index.name == 'fips'):
        county_path_style = 'font-size:12px;fill-rule:nonzero;stroke:#FFFFFF;stroke-opacity:1.0;fill-opacity:1.0;stroke-width:0.2;stroke-miterlimit:4;stroke-dasharray:none;stroke-linecap:butt;marker-start:none;stroke-linejoin:bevel;fill:'
    elif data.index.name == 'state_fips':
        county_path_style = 'font-size:12px;fill-rule:nonzero;stroke:#FFFFFF;stroke-opacity:0.0;fill-opacity:1.0;stroke-width:0.2;stroke-miterlimit:4;stroke-dasharray:none;stroke-linecap:butt;marker-start:none;stroke-linejoin:bevel;fill:'
    
    #make color palette
    if color == 'cube':
        pal = sns.cubehelix_palette(as_cmap=True) 
        missing_path_style = 'font-size:12px;fill-rule:nonzero;stroke:#737373;stroke-opacity:1.0;fill-opacity:1.0;stroke-width:0.1;stroke-miterlimit:4;stroke-dasharray:none;stroke-linecap:butt;marker-start:none;stroke-linejoin:bevel;fill:#737373'
    else:
        pal = sns.light_palette(color, as_cmap=True) 
        missing_path_style = 'font-size:12px;fill-rule:nonzero;stroke:#000000;stroke-opacity:1.0;fill-opacity:1.0;stroke-width:0.1;stroke-miterlimit:4;stroke-dasharray:none;stroke-linecap:butt;marker-start:none;stroke-linejoin:bevel;fill:#000000'
    
    cNorm  = colors.Normalize(vmin = data.quantile(0.05), vmax = data.quantile(0.95))
    scalarMap = cmx.ScalarMappable(norm=cNorm, cmap=pal)
        
    data = data.apply(scalarMap.to_rgba)
    data = data.apply(colors.rgb2hex)
    
    for p in county_paths:     
        if (data.index.name == 'zip3') | (data.index.name == 'zip_code'):      
            lookup = fips_to_zip[p['id']]
        elif data.index.name == 'state_fips':
            lookup = p['id'][:2]
        elif data.index.name == 'fips':
            lookup = p['id']
            
        if lookup in data:
            p['style'] = county_path_style + data[lookup]
        else: 
            p['style'] = missing_path_style
    
    if get_cbar:
        cbar_fig,ax=plt.subplots(1,1,figsize=(6,1))
        cb1 = cbar.ColorbarBase(ax,cmap=pal, norm=cNorm, orientation='horizontal')  
        name_legend_map = {'counts': 'Number of loans (thousands)',
				   'ROI': 'ROI (%)',
				   'int_rate': 'interest rate (%)',
				   'default_prob': 'default probability',
				   'dti': 'Debt-to-income ratio',
				   'emp_length':'employment length (months)',
                          'annual_inc':'annual income ($)'}
        if agg_fun == 'count':
            label = 'Number of loans (thousands)'
        else:
            agg_fun_map = {'mean':'avg. ',
                           'median':'median ',
                           'std':'SD of '}
            label = agg_fun_map[agg_fun] + name_legend_map[data.name]
        cb1.set_label(label)
        plt.tight_layout()
           
        return cbar_fig 
    
    
def compute_group_avgs(df, col_name, group_by, agg_fun='mean', 
                       state_fips_dict = None, min_counts = 50):
    """get series of group-avgs based on either addr_state or zip3 """
    group_counts = df.groupby(group_by)['int_rate'].count()
    if agg_fun=='count': #if we want counts just replace avgs with this
        group_avgs = group_counts/1000 #keep in thousands
    elif agg_fun=='mean':
        group_avgs = df.groupby(group_by)[col_name].mean()
    elif agg_fun=='median':
        group_avgs = df.groupby(group_by)[col_name].median()
    elif agg_fun=='std':
        group_avgs = df.groupby(group_by)[col_name].std()
    if agg_fun != 'count':
        group_avgs = group_avgs[group_counts >= min_counts] 
    
    if group_by == 'addr_state':
        group_avgs.index = group_avgs.index.map(lambda x: state_fips_dict[x])
        group_avgs.index.name = 'state_fips'
        
    return group_avgs   

#%% generating figures
def make_strat_returns_fig(LD, group_by = None, K_prctile=25, smooth_span = 5):
    '''Make figure showing how much better you do if you pick from the 
    historically best geographic regions'''
    
    import datetime
    plot_col = 'weighted_ROI'
    counts_by_date = LD.groupby('issue_d')['issue_d'].count()
    start_date = np.datetime64(datetime.datetime(2009,01,01),'ns')
    counts_by_date = counts_by_date[counts_by_date.index > start_date]
    unique_dates = np.sort(counts_by_date.index.unique())
    
    if group_by:
        K_num = np.round(len(LD[group_by].unique())*K_prctile/100) #pick best K from group
        best_state_avgs = np.zeros(len(unique_dates),)
        worst_state_avgs = np.zeros(len(unique_dates),)
        marg_avgs = np.zeros(len(unique_dates),)
        for idx,up_to in enumerate(unique_dates):
            states_up_to = LD[LD.issue_d < up_to].groupby(group_by)[plot_col].mean()
            curr_data = LD.ix[LD.issue_d == up_to,[plot_col,group_by]] #loans issued at this date
            marg_avgs[idx] = curr_data[plot_col].mean()
        
            best_states = states_up_to.sort_values(ascending=False).head(K_num).index.values
            worst_states = states_up_to.sort_values(ascending=False).tail(K_num).index.values
            best_state_avgs[idx] = curr_data.ix[curr_data[group_by].isin(best_states),plot_col].mean()
            worst_state_avgs[idx] = curr_data.ix[curr_data[group_by].isin(worst_states),plot_col].mean()
        
        moving_avgs = pd.DataFrame({'counts':counts_by_date,'marg':marg_avgs,'best':best_state_avgs},index=unique_dates)
        moving_avgs = pd.ewma(moving_avgs,span=smooth_span)
        moving_avgs['rel_imp'] = 100*(moving_avgs['best'] - moving_avgs['marg']) / moving_avgs['marg']
    else:
        moving_avgs = pd.DataFrame({'counts':counts_by_date},index=unique_dates)   
        moving_avgs = pd.ewma(moving_avgs,span=smooth_span)
   
    fig,ax1 = plt.subplots(figsize = (6.0,4.0))
    ax1.plot(unique_dates,moving_avgs['counts']/1000,'b')
    ax1.set_ylabel('Number of loans (thousands)', color='b',fontsize=16)
    ax1.set_xlabel('Loan issue date')
    for tl in ax1.get_yticklabels():
        tl.set_color('b')     
    
    if group_by: #if were computing group avgs
        ax2 = ax1.twinx()
        ax2.plot(unique_dates,moving_avgs['rel_imp'],'r')
        ax2.set_ylabel('Relative improvement (%)', color='r',fontsize=16)
        ax2.axhline(y=0.,color='k')
        for tl in ax2.get_yticklabels():
            tl.set_color('r')         
        ax1.get_yaxis().set_ticks([])
    plt.tight_layout()
    
    return fig  
  
#def make_png_response(fig):
#    ''' Make mpl figure into a png object to feed into html '''    
#    import StringIO
#    from flask import make_response
#    from matplotlib.backends.backend_agg import FigureCanvasAgg as FigureCanvas
#    canvas=FigureCanvas(fig)
#    png_output = StringIO.StringIO()
#    canvas.print_png(png_output)
#    response= make_response(png_output.getvalue())
#    response.headers['Content-Type'] = 'image/png'
#    return response