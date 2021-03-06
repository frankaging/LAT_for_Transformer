from __future__ import division
from __future__ import print_function
from __future__ import absolute_import

import sys, os, shutil
import argparse
import copy
import csv
import pandas as pd
import numpy as np
from scipy.stats import pearsonr
import matplotlib.pyplot as plt

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.utils.data import DataLoader

from datasets import seq_collate_dict, load_dataset
from models import *
from random import shuffle
from operator import itemgetter
import pprint
from numpy import newaxis as na

from string import punctuation
import statistics 

import logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(message)s",
    handlers=[
        logging.StreamHandler()
    ])
logger = logging.getLogger()

def count_parameters(model):
    return sum(p.numel() for p in model.parameters() if p.requires_grad)

def eval_ccc(y_true, y_pred):
    """Computes concordance correlation coefficient."""
    true_mean = np.mean(y_true)
    true_var = np.var(y_true)
    pred_mean = np.mean(y_pred)
    pred_var = np.var(y_pred)
    covar = np.cov(y_true, y_pred, bias=True)[0][1]
    ccc = 2*covar / (true_var + pred_var +  (pred_mean-true_mean) ** 2)
    return ccc

def chunks(l, n):
    """Yield successive n-sized chunks from l."""
    for i in range(0, len(l), n):
        yield l[i:i + n]

'''
helper to chunknize the data for each a modality
'''
def generateInputChunkHelper(data_chunk, length_chunk, tensor=True):
    # sort the data with length from long to short
    combined_data = list(zip(data_chunk, length_chunk))
    combined_data.sort(key=itemgetter(1),reverse=True)
    data_sort = []
    for pair in combined_data:
        data_sort.append(pair[0])

    if tensor:
        # produce the operatable tensors
        data_sort_t = torch.tensor(data_sort, dtype=torch.float)
        return data_sort_t
    else:
        return data_sort

'''
yielding training batch for the training process
'''
def generateTrainBatch(input_data, input_target, input_length, token_lengths, args, batch_size=25):
    # TODO: support input_data as a dictionary
    # get chunk
    input_size = len(input_data[list(input_data.keys())[0]]) # all values have same size
    index = [i for i in range(0, input_size)]
    if batch_size != 1:
        shuffle(index)
    shuffle_chunks = [i for i in chunks(index, batch_size)]
    for chunk in shuffle_chunks:
        # chunk yielding data
        yield_input_data = {}
        # same across a single chunk
        target_chunk = [input_target[index] for index in chunk]
        length_chunk = [input_length[index] for index in chunk]
        token_length_chunk = [token_lengths[index] for index in chunk]
        # max length
        max_length = max(length_chunk)
        # max token length
        max_token_length = max([max(chunk) for chunk in token_length_chunk])
        # mod data generating
        for mod in list(input_data.keys()):
            data_chunk = [input_data[mod][index] for index in chunk]
            data_chunk_sorted = \
                generateInputChunkHelper(data_chunk, length_chunk)
            data_chunk_sorted = data_chunk_sorted[:,:max_length,:max_token_length,:]
            yield_input_data[mod] = data_chunk_sorted
        # target generating
        target_sort = \
            generateInputChunkHelper(target_chunk, length_chunk)
        target_sort = target_sort[:,:max_length]
        # token length generating
        token_length_sort = \
            generateInputChunkHelper(token_length_chunk, length_chunk, tensor=False)

        # mask generation for the whole batch
        lstm_masks = torch.zeros(target_sort.size()[0], target_sort.size()[1], 1, dtype=torch.float)
        length_chunk.sort(reverse=True)
        for i in range(lstm_masks.size()[0]):
            lstm_masks[i,:length_chunk[i]] = 1

        # yielding for each batch
        yield (yield_input_data, torch.unsqueeze(target_sort, dim=2), lstm_masks, length_chunk, token_length_sort)

def evaluateOnEval(input_data, input_target, lengths, token_lengths, model, criterion, args, fig_path=None):
    model.eval()
    predictions = []
    weights_total = []
    gs_total = []
    tf_attns_total = []
    ctx_attns_total = []
    actuals = []
    data_num = 0
    loss, ccc = 0.0, []
    count = 0
    index = 0
    total_vid_count = len(input_data[list(input_data.keys())[0]])
    for (data, target, mask, lengths, token_lengths) in generateTrainBatch(input_data,
                                                            input_target,
                                                            lengths,
                                                            token_lengths, 
                                                            args,
                                                            batch_size=1):
        print("Video #: " + str(index+1) + "/" + str(total_vid_count))
        # send to device
        mask = mask.to(args.device)
        # send all data to the device
        for mod in list(data.keys()):
            data[mod] = data[mod].to(args.device)
            data[mod] = Variable(data[mod], requires_grad=True)
        target = target.to(args.device)
        # Run forward pass
        output = model.forward(data, lengths, token_lengths, mask)
        # Also get the weight
        weights = model.backward_nlap(data, lengths, token_lengths, mask)
        tf_weights, ctx_weights = model.backward_tf_attn(data, lengths, token_lengths, mask)
        tf_attns_total.append(tf_weights)
        ctx_attns_total.append(ctx_weights)
        weights_total.append(weights)

        # get gradient w.r.t. inputs here
        output.backward(torch.ones_like(output))
        grad_sa = (data[mod].grad**2).sum(dim=-1).squeeze(dim=0)
        gs_total.append(grad_sa) 

        predictions.append(output.reshape(-1).tolist())
        actuals.append(target.reshape(-1).tolist())
        # Compute loss
        loss += criterion(output, target)
        # Keep track of total number of time-points
        data_num += sum(lengths)
        # Compute correlation and CCC of predictions against ratings
        output = torch.squeeze(torch.squeeze(output, dim=2), dim=0).cpu().detach().numpy()
        target = torch.squeeze(torch.squeeze(target, dim=2), dim=0).cpu().detach().numpy()
        if count == 0:
            # print(output)
            # print(target)
            count += 1
        curr_ccc = eval_ccc(output, target)
        ccc.append(curr_ccc)
        index += 1
    # Average losses and print
    loss /= data_num
    return ccc, predictions, actuals, weights_total, tf_attns_total, ctx_attns_total, gs_total

def plot_predictions(dataset, predictions, metric, args, fig_path=None):
    """Plots predictions against ratings for representative fits."""
    # Select top 4 and bottom 4
    sel_idx = np.concatenate((np.argsort(metric)[-4:][::-1],
                              np.argsort(metric)[:4]))
    sel_metric = [metric[i] for i in sel_idx]
    sel_true = [dataset.orig['ratings'][i] for i in sel_idx]
    sel_pred = [predictions[i] for i in sel_idx]
    for i, (true, pred, m) in enumerate(zip(sel_true, sel_pred, sel_metric)):
        j, i = (i // 4), (i % 4)
        args.axes[i,j].cla()
        args.axes[i,j].plot(true, 'b-')
        args.axes[i,j].plot(pred, 'c-')
        args.axes[i,j].set_xlim(0, len(true))
        args.axes[i,j].set_ylim(-1, 1)
        args.axes[i,j].set_title("Fit = {:0.3f}".format(m))
    plt.tight_layout()
    plt.draw()
    if fig_path is not None:
        plt.savefig(fig_path)
    plt.pause(1.0 if args.test else 0.001)

def plot_eval(pred_sort, ccc_sort, actual_sort, window_size=1):
    sub_graph_count = len(pred_sort)
    fig = plt.figure()
    fig.subplots_adjust(hspace=0.4, wspace=0.4)

    for i in range(1, 7):
        ax = fig.add_subplot(2, 3, i)

        ccc = ccc_sort[i-1]
        pred = pred_sort[i-1]
        actual = actual_sort[i-1]
        minL = min(len(pred), len(actual))
        pred = pred[:minL]
        actual = actual[:minL]
        t = []
        curr_t = 0.0
        for i in pred:
            t.append(curr_t)
            curr_t += window_size
        pred_line, = ax.plot(t, pred, '-' , color='r', linewidth=2.0, label='Prediction')
        ax.legend()
        actual_line, = ax.plot(t, actual, '-', color='b', linewidth=2.0, label='True')
        ax.legend()
        ax.set_ylabel('valence(0-10)')
        ax.set_xlabel('time(s)')
        ax.set_title('ccc='+str(ccc)[:5])
    plt.show()
    # plt.savefig("./lstm_save/top_ccc.png")

def save_predictions(dataset, predictions, path):
    for p, seq_id in zip(predictions, dataset.seq_ids):
        df = pd.DataFrame(p, columns=['rating'])
        fname = "target_{}_{}_normal.csv".format(*seq_id)
        df.to_csv(os.path.join(path, fname), index=False)

def save_params(args, model, train_stats, test_stats):
    fname = 'param_hist.tsv'
    df = pd.DataFrame([vars(args)], columns=vars(args).keys())
    df = df[['modalities', 'batch_size', 'split', 'epochs', 'lr',
             'sup_ratio', 'base_rate']]
    for k in ['ccc_std', 'ccc']:
        v = train_stats.get(k, float('nan'))
        df.insert(0, 'train_' + k, v)
    for k in ['ccc_std', 'ccc']:
        v = test_stats.get(k, float('nan'))
        df.insert(0, 'test_' + k, v)
    df.insert(0, 'model', [model.__class__.__name__])
    df['embed_dim'] = model.embed_dim
    df['h_dim'] = model.h_dim
    df['attn_len'] = model.attn_len
    df['ar_order'] = [float('nan')]
    df.set_index('model')
    df.to_csv(fname, mode='a', header=(not os.path.exists(fname)), sep='\t')

def save_checkpoint(modalities, mod_dimension, window_size, model, path):
    checkpoint = {'modalities': modalities, 'mod_dimension' : mod_dimension, 'window_size' : window_size, 'model': model.state_dict()}
    torch.save(checkpoint, path)

def load_checkpoint(path, device):
    checkpoint = torch.load(path, map_location=device)
    return checkpoint

def load_data(modalities, data_dir, eval_dir=None):
    print("Loading data...")
    if eval_dir == None:
        train_data = load_dataset(modalities, data_dir, 'Train',
                                truncate=True, item_as_dict=True)
        test_data = load_dataset(modalities, data_dir, 'Valid',
                                truncate=True, item_as_dict=True)
        print("Done.")
        return train_data, test_data
    eval_data = load_dataset(modalities, data_dir, eval_dir,
                             truncate=True, item_as_dict=True)
    print("Loading Eval Set Done.")
    return eval_data

def textInputHelper(input_data, window_size, channel):
    # channel features
    vectors_raw = input_data[channel]
    ts = input_data["linguistic_timer"]

    #  get the window size and repeat rate if oversample is needed
    oversample = int(window_size[channel]/window_size['ratings'])
    window_size = window_size[channel]

    video_vs = []
    count_v = 0
    current_time = 0.0
    window_vs = []
    while count_v < len(vectors_raw):
        t = ts[count_v]
        if type(t) == list:
            t = t[0]
        if t <= current_time + window_size:
            window_vs.append(vectors_raw[count_v])
            count_v += 1
        else:
            if len(window_vs) == 0:
                window_vs.append('null')
            for i in range(0, oversample):
                video_vs.append(window_vs)
            window_vs = []
            current_time += window_size
    return video_vs

def videoInputHelper(input_data, window_size, channel):
    # channel features
    vectors_raw = input_data[channel]
    ts = input_data["linguistic_timer"]
    # remove nan values
    vectors = []
    for vec in vectors_raw:
        inner_vec = []
        for v in vec:
            if np.isnan(v):
                inner_vec.append(0)
            else:
                inner_vec.append(v)
        vectors.append(inner_vec)

    #  get the window size and repeat rate if oversample is needed
    oversample = int(window_size[channel]/window_size['ratings'])
    window_size = window_size[channel]
    mod_dimension = {'linguistic' : 300}

    video_vs = []
    count_v = 0
    current_time = 0.0
    window_vs = []
    while count_v < len(vectors):
        t = ts[count_v]
        if type(t) == list:
            t = t[0]
        if t <= current_time + window_size:
            window_vs.append(vectors[count_v])
            count_v += 1
        else:
            if len(window_vs) == 0:
                if count_v > 0 :
                    # append last one again
                    pad_vec = np.copy(vectors[count_v-1])
                    window_vs.append(pad_vec)
                else:
                    # or enforce append first element
                    if len(vectors) != 0:
                        pad_vec = np.copy(vectors[0])
                        window_vs.append(pad_vec)
                    else:
                        # or simply 0 like will never happened!
                        pad_vec = [0.0] * mod_dimension[channel]
                        pad_vec = np.array(pad_vec)
                        window_vs.append(pad_vec)
            for i in range(0, oversample):
                temp = np.array(window_vs)
                video_vs.append(temp)
            window_vs = []
            current_time += window_size
    # TODO: we are only taking average from each window for image
    #if channel == 'image':
    # data = np.asarray(video_vs)
    # data = np.average(data, axis=1)
    # video_vs = np.expand_dims(data, axis=1).tolist()
    return video_vs

def ratingInputHelper(input_data, window_size):
    ratings = input_data['ratings']
    ts = input_data['ratings_timer']
    window_size = window_size['ratings']

    current_time = 0.0
    count_r = 0
    window_rs = []
    video_rs = []
    while count_r < len(ratings):
        t = ts[count_r]
        if t <= current_time + window_size:
            window_rs.append(ratings[count_r])
            count_r += 1
        else:
            avg_r = sum(window_rs)*1.0/len(window_rs)
            video_rs.append(avg_r)
            window_rs = []
            current_time += window_size
    return video_rs

'''
Construct inputs for different channels: emotient, linguistic, ratings, etc..
'''
def constructInput(input_data, window_size, channels):
    ret_input_features = {}
    ret_ratings = []
    for data in input_data:
        # print(data['linguistic_timer'])
        # channel features
        minL = 99999999
        for channel in channels:
            if channel != "linguistic_text":
                video_vs = videoInputHelper(data, window_size, channel)
            else:
                video_vs = textInputHelper(data, window_size, channel)
            if channel not in ret_input_features.keys():
                ret_input_features[channel] = []
            ret_input_features[channel].append(video_vs)
            if len(video_vs) < minL:
                minL = len(video_vs)
        video_rs = ratingInputHelper(data, window_size)
        # print("video_rs vector size: " + str(len(video_rs)))
        if len(video_rs) < minL:
            minL = len(video_rs)
        # concate
        for channel in channels:
             ret_input_features[channel][-1] = ret_input_features[channel][-1][:minL]
        ret_ratings.append(video_rs[:minL])
    return ret_input_features, ret_ratings

def padInputHelper(input_data, dim, old_version=False):
    output = []
    max_num_vec_in_window = 0
    max_num_windows = 0
    seq_lens = []
    token_lens = []
    for data in input_data:
        if max_num_windows < len(data):
            max_num_windows = len(data)
        seq_lens.append(len(data))
        if max_num_vec_in_window < max([len(w) for w in data]):
            max_num_vec_in_window = max([len(w) for w in data])
        token_lens.append([len(w) for w in data])

    padVec = [0.0]*dim
    for vid in input_data:
        vidNewTmp = []
        for wind in vid:
            windNew = [padVec] * max_num_vec_in_window
            windNew[:len(wind)] = wind
            vidNewTmp.append(windNew)
        vidNew = [[padVec] * max_num_vec_in_window]*max_num_windows
        vidNew[:len(vidNewTmp)] = vidNewTmp
        output.append(vidNew)
    return output, seq_lens, token_lens

'''
pad every sequence to max length, also we will be padding windows as well
'''
def padInput(input_data, channels, dimensions):
    # input_features <- list of dict: {channel_1: [117*features],...}
    ret = {}
    seq_lens = []
    for channel in channels:
        pad_channel, seq_lens, token_lens = padInputHelper(input_data[channel], dimensions[channel])
        ret[channel] = pad_channel
    return ret, seq_lens, token_lens
def getSeqList(seq_ids):
    ret = []
    for seq_id in seq_ids:
        ret.append(seq_id[0]+"_"+seq_id[1])
    return ret
'''
pad targets
'''
def padRating(input_data, max_len):
    output = []
    # pad ratings
    for rating in input_data:
        ratingNew = [0]*max_len
        ratingNew[:len(rating)] = rating
        output.append(ratingNew)
    return output

def softmax(x_in, axis=None):
    x = np.array(x_in)
    x = x - x.max(axis=axis, keepdims=True)
    y = np.exp(x)
    return y / y.sum(axis=axis, keepdims=True)

def normalize_w(x):
    if max(x) == min(x):
        return [sum(x)*1.0/len(x) for w in x]
    else:
        return [(w - min(x))*1.0/(max(x) - min(x)) for w in x]

def normalize_lap(x):
    abs_w = [abs(w) for w in x]
    return [w*1.0/max(abs_w) for w in x]

def SEND(args):
    # Fix random seed
    torch.manual_seed(1)
    torch.cuda.manual_seed(1)
    np.random.seed(1)

    # clear memory
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    # Convert device string to torch.device
    args.device = (torch.device(args.device) if torch.cuda.is_available()
                   else torch.device('cpu'))

    args.modalities = ['linguistic']
    mod_dimension = {'linguistic' : 300}
    window_size = {'linguistic' : 5, 'ratings' : 5, 'linguistic_text' : 5}

    # loss function define
    criterion = nn.MSELoss(reduction='sum')
    # construct model and params setting
    eval_dir = "Test"
    model_path = args.model_path
    model = TransformerLSTMAttn(mods=args.modalities, dims=mod_dimension, device=args.device)
    # Setting the optimizer
    optimizer = optim.Adam(model.parameters(), lr=args.lr, weight_decay=1e-4)

    # print(count_parameters(model))
    # return

    print("evaluating on the " + eval_dir + " Set.")
    eval_data = load_data(args.modalities, args.data_dir, eval_dir)

    seq_ids = getSeqList(eval_data.seq_ids)
    input_features_eval, ratings_eval = \
        constructInput(eval_data,
                       channels=['linguistic', 'linguistic_text'],
                       window_size=window_size)

    # separate out text data
    saved_text = copy.deepcopy(input_features_eval['linguistic_text']) 
    seq_ls = []
    w_l = []
    for seq in saved_text:
        seq_l = 0
        for w in seq:
            seq_l += len(w)
            w_l.append(len(w))
        seq_ls.append(seq_l)

    print("Seq Min %s, Max %s, AVG %s, STD %s" % (str(min(seq_ls)), \
                                                  str(max(seq_ls)), \
                                                  str((sum(seq_ls)*1.0/len(seq_ls))), \
                                                  str(statistics.stdev(seq_ls))))

    print("Window Min %s, Max %s, AVG %s, STD %s" % (str(min(w_l)), \
                                                  str(max(w_l)), \
                                                  str((sum(w_l)*1.0/len(w_l))), \
                                                  str(statistics.stdev(w_l))))


    del input_features_eval['linguistic_text']
    input_padded_eval, seq_lens_eval, token_lens_eval = padInput(input_features_eval, args.modalities, mod_dimension)
    ratings_padded_eval = padRating(ratings_eval, max(seq_lens_eval))

    # load model
    checkpoint = load_checkpoint(model_path, args.device)
    model.load_state_dict(checkpoint['model'])

    # evalution
    ccc, pred, actuals, weights_total, tf_attns_total, ctx_attns_total, gs_total = \
        evaluateOnEval(input_padded_eval, ratings_padded_eval, seq_lens_eval, token_lens_eval,
                        model, criterion, args)
    stats = {'ccc': np.mean(ccc), 'ccc_std': np.std(ccc)}
    logger.info('Evaluation\tCCC(std): {:2.5f}({:2.5f})'.\
        format(stats['ccc'], stats['ccc_std']))

    # get top ccc
    seq_ids = getSeqList(eval_data.seq_ids)
    seq_ccc = list(zip(seq_ids, ccc))
    seq_ccc = sorted(seq_ccc,key=lambda x:(-x[1],x[0]))

    seq_ccc = seq_ccc
    seq_index = []
    for item in seq_ccc:
        _i = 0
        for _id in seq_ids:
            if _id == item[0]:
                seq_index.append(_i)
                break
            _i += 1

    # print out the word weight mappings
    print("Writing to files...")
    post_file = "_send.csv"
    word_level_w = dict()
    word_level_gs = dict()
    word_level_c = dict()

    # save id to weights mapping for other plots
    weights_plot = dict()
    labels_plot = dict()
    sentence_plot = dict()
    gs_plot = dict()

    tf_attns_plot = dict()
    ctx_attns_plot = dict()

    seq_ccc_plot = dict()

    for i in range(len(seq_ccc)):
        actual_r = actuals[seq_index[i]]
        pred_r = pred[seq_index[i]]

        # temp plot
        # t = []
        # curr_t = 0.0
        # for ii in actual_r:
        #     t.append(curr_t)
        #     curr_t += 5
        # fig = plt.figure()
        # plt.plot(t, pred_r, '-' , color='r', linewidth=2.0, label='Prediction')
        # plt.legend()
        # plt.plot(t, actual_r, '--' , color='b', linewidth=2.0, label='Actual')
        # plt.legend()
        # plt.savefig('../send_plots/' +  seq_ccc[i][0] + '_' + str(seq_ccc[i][1])[:5] + '_valence.png')
        # plt.close(fig)

        weights = weights_total[seq_index[i]]
        gs = gs_total[seq_index[i]]
        word = saved_text[seq_index[i]]

        tf_attn = tf_attns_total[seq_index[i]]
        ctx_attn = ctx_attns_total[seq_index[i]]

        # print(word)
        ccc = seq_ccc[i][1]
        seq_id = seq_ccc[i][0]
        print("Video Id: " + seq_id + " with CCC: " + str(ccc))
        seq_ccc_plot[seq_id] = ccc

        if seq_id not in labels_plot.keys():
            labels_plot[seq_id] = []
        for iii in range(len(actual_r)):
            labels_plot[seq_id].append((actual_r[iii], pred_r[iii]))

        for t in range(len(saved_text[seq_index[i]])):
            row = [(t+1)*5.0, actual_r[t], pred_r[t]]
            word_t = [w.strip().strip(punctuation).lower() for w in word[t]]

            word_w = weights[t].tolist()[:len(word_t)]
            word_gs = gs[t].tolist()[:len(word_t)]

            norm_word_w_t = softmax(word_w).tolist()
            norm_word_gs_t = softmax(word_gs).tolist()
            # norm_word_w_t = normalize_lap(word_w)
            # print(norm_word_w_t)

            # if len(word_t) < 5:
            #     continue
            if seq_id not in weights_plot.keys():
                weights_plot[seq_id] = []
                sentence_plot[seq_id] = []
                tf_attns_plot[seq_id] = []
                ctx_attns_plot[seq_id] = []
                gs_plot[seq_id] = []

            weights_plot[seq_id].append(norm_word_w_t)
            sentence_plot[seq_id].append(word_t)
            gs_plot[seq_id].append(norm_word_gs_t)

            tf_attns_plot[seq_id].append(tf_attn[t,:,:,:len(word_t),:len(word_t)].tolist())
            ctx_attns_plot[seq_id].append(ctx_attn[t,:len(word_t)].tolist())

            for i in range(len(word_t)):
                # normalize by the length as well
                w_i = 1.0 * norm_word_w_t[i]
                gs_i = 1.0 * norm_word_gs_t[i]
                w_s = word_t[i]
                if w_s not in word_level_w.keys():
                    word_level_w[w_s] = [w_i]
                    word_level_gs[w_s] = [gs_i]
                else:
                    word_level_w[w_s].append(w_i)
                    word_level_gs[w_s].append(gs_i)
                if w_s not in word_level_c.keys():
                    word_level_c[w_s] = 1
                else:
                    word_level_c[w_s] = word_level_c[w_s] + 1

    output_file = args.out_dir + "/words_" + eval_dir + post_file
    word_level_w = [(k, v) for k, v in word_level_w.items()] 
    
    with open(output_file, mode='w') as csv_file:
        file_writer = csv.writer(csv_file, delimiter=',')
        header = ["word", "count", "sum", "avg", "std", "sum_gs", "avg_gs", "std_gs"]
        file_writer.writerow(header)
        for kv in word_level_w:
            var = 0.0
            var_gs = 0.0
            kv_gs = word_level_gs[kv[0]]
            if len(kv[1]) > 1:
                var = statistics.stdev(kv[1])
                var_gs = statistics.stdev(kv_gs)
            row = [kv[0], word_level_c[kv[0]], sum(kv[1]),
                   sum(kv[1])*1.0/word_level_c[kv[0]], var,
                   sum(kv_gs), sum(kv_gs)*1.0/word_level_c[kv[0]], var_gs]
            file_writer.writerow(row)

    # save id to weight mapping
    import pickle
    # print(labels_plot)
    # print(sentence_plot)
    pickle.dump( weights_plot, open(args.out_dir + "/seq_weights_test_send.p", "wb") )
    pickle.dump( gs_plot, open(args.out_dir + "/seq_gs_test_send.p", "wb") )
    pickle.dump( labels_plot, open(args.out_dir + "/seq_labels_test_send.p", "wb") )
    pickle.dump( sentence_plot, open(args.out_dir + "/seq_sentences_test_send.p", "wb") )
    pickle.dump( tf_attns_plot, open(args.out_dir + "/seq_tf_attns_test_send.p", "wb") )
    pickle.dump( ctx_attns_plot, open(args.out_dir + "/seq_ctx_attns_test_send.p", "wb") )
    pickle.dump( seq_ccc_plot, open(args.out_dir + "/seq_ccc_test_send.p", "wb") )

    return None

def generate_class(raw_in):
    _class = dict()
    for seq in raw_in.keys():
        rate = raw_in[seq]
        if rate >= 0.0 and rate <= 0.2:
            _class[seq] = 1
        elif rate > 0.2 and rate <= 0.4:
            _class[seq] = 2
        elif rate > 0.4 and rate <= 0.6:
            _class[seq] = 3
        elif rate > 0.6 and rate <= 0.8:
            _class[seq] = 4
        elif rate > 0.8 and rate <= 1.0:
            _class[seq] = 5
        else:
            assert(False)
    return _class

def padFeaturesSST(features):
    # get max length
    seq_len = [len(seq) for seq in features]
    seq_len_max = max(seq_len)
    pad_vec = [0.0]*300
    padded_feature = []
    for seq in features:
        temp_feature = [pad_vec] * seq_len_max
        temp_feature[:len(seq)] = seq
        padded_feature.append(temp_feature)
    return padded_feature, seq_len
        
def sortSST(data_chunk, length_chunk, tensor=True):
    # sort the data with length from long to short
    combined_data = list(zip(data_chunk, length_chunk))
    combined_data.sort(key=itemgetter(1),reverse=True)
    data_sort = []
    for pair in combined_data:
        data_sort.append(pair[0])
    if tensor:
        # produce the operatable tensors
        data_sort_t = torch.tensor(data_sort, dtype=torch.float)
        return data_sort_t
    return data_sort

def generateBatchSST(input_data, input_target, seq_ids, args, batch_size=1):
    # select batch sentence id
    index = [i for i in range(0, len(seq_ids))]
    shuffle_chunks = [i for i in chunks(index, batch_size)] # contains array index
    for chunk in shuffle_chunks:
        chunk_ids = [seq_ids[index] for index in chunk]
        # sort feature
        features = [input_data[_id] for _id in chunk_ids]
        padded_feature, seq_len = padFeaturesSST(features)
        sort_feature = sortSST(padded_feature, seq_len)
        # sort target
        targets = [input_target[_id] for _id in chunk_ids]
        sort_targets = sortSST(targets, seq_len)
        # sorted chunk id
        sort_chunk_ids = sortSST(chunk_ids, seq_len, tensor=False)
        # sort length
        seq_len.sort(reverse=True)
        # mask
        batch_size = len(seq_len)
        max_len = max(seq_len)
        mask = torch.zeros(batch_size, max_len, dtype=torch.float)
        for i in range(mask.size()[0]):
            mask[i,:seq_len[i]] = 1
        yield sort_feature, sort_targets, seq_len, mask, sort_chunk_ids

def oneHotVector(sort_targets):
    n_class = 5
    batch_size = sort_targets.shape[0]
    target = torch.zeros((batch_size, int(n_class)), dtype=torch.float).to(args.device)
    for i in range(batch_size):
        target[i][int(sort_targets[i]) - 1] = 1.0
    return target

def calculate_accuracy(predict, actual):
    _, predicted = torch.max(predict, dim=-1)
    multiclass = 0
    for i in range(predict.shape[0]):
        if int(predicted[i]) == int(actual[i]) - 1:
            multiclass += 1

    binary = 0
    binary_total = 0 # will not count nuetural cases
    for i in range(predict.shape[0]):
        if int(actual[i]) != 3:
            binary_total += 1
            if int(actual[i]) < 3 and int(predicted[i]) < 2:
                binary += 1
            elif int(actual[i]) > 3 and int(predicted[i]) > 2:
                binary += 1

    return multiclass, binary, binary_total

def stringOut(sort_targets, output):
    _, predicted = torch.max(output, dim=-1)
    ret = []
    for i in range(sort_targets.shape[0]):
        actual = ''
        if int(sort_targets[i]) == 5:
            actual = "$++$"
        elif int(sort_targets[i]) == 4:
            actual = "$+$"
        elif int(sort_targets[i]) == 3:
            actual = "$.$"
        elif int(sort_targets[i]) == 2:
            actual = "$-$"
        elif int(sort_targets[i]) == 1:
            actual = "$--$"
        predict = ''
        if int(predicted[i]) == 4:
            predict = "$++$"
        elif int(predicted[i]) == 3:
            predict = "$+$"
        elif int(predicted[i]) == 2:
            predict = "$.$"
        elif int(predicted[i]) == 1:
            predict = "$-$"
        elif int(predicted[i]) == 0:
            predict = "$--$"
        ret.append((actual, predict))
    return ret

def SST(args):
    print("Start analyzing SST trained models ...")
    # Fix random seed
    torch.manual_seed(1)
    torch.cuda.manual_seed(1)
    np.random.seed(1)

    # clear memory
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    # Convert device string to torch.device
    args.device = (torch.device(args.device) if torch.cuda.is_available()
                   else torch.device('cpu'))

    # Loading the data
    print("Loading SST data ...")
    data_folder = args.data_dir
    import pickle
    test_data = pickle.load( open( data_folder + "id_embed_test.p", "rb" ) )
    test_target = pickle.load( open( data_folder + "id_rating_test.p", "rb" ) )
    all_sentence = pickle.load( open( data_folder + "id_sentence.p", "rb" ) )
    assert(len(test_data) == len(test_target))
    print("Verified SST data ...")
    print("Test Set Size: ", len(test_data))

    seq_ls = []
    for _id in test_data.keys():
        seq_ls.append(len(all_sentence[_id]))

    print("Seq Min %s, Max %s, AVG %s, STD %s" % (str(min(seq_ls)), \
                                                  str(max(seq_ls)), \
                                                  str((sum(seq_ls)*1.0/len(seq_ls))), \
                                                  str(statistics.stdev(seq_ls))))

    # set the prediction categories
    test_class = generate_class(test_target)

    args.modalities = ['linguistic']
    mod_dimension = {'linguistic' : 300}
    # loss function define
    criterion = nn.BCELoss(reduction='sum')
    # construct model
    model = TransformerLinearAttn(mods=args.modalities, dims=mod_dimension, device=args.device)
    # load model
    model_path = args.model_path
    checkpoint = load_checkpoint(model_path, args.device)
    model.load_state_dict(checkpoint['model'])

    # print(count_parameters(model))
    # return

    loss = 0.0
    best_multi_acur = -1.0
    best_binary_acur = -1.0
    args.batch_size = 500
    multiclass_correct = 0
    binary_correct = 0
    multiclass_instance = 0
    binary_instance = 0
    weights = []
    gradients = []
    seq_ids = [k for k in test_data.keys()]
    sort_seq_ids = []

    model.eval()
    # save for output labels
    stringOuts = []
    print("Running forward step to extract weights ...")
    # get the loss of test set
    model.zero_grad()
    # for each epoch do the training
    for sort_feature, sort_targets, seq_len, mask, sort_chunk_ids in \
        generateBatchSST(test_data, test_class, seq_ids, args, batch_size=500):
        # send to device
        mask = mask.to(args.device)
        sort_feature = sort_feature.to(args.device)
        sort_targets = sort_targets.to(args.device)
        # Run forward pass.
        sort_feature = Variable(sort_feature, requires_grad=True)
        output = model(sort_feature, seq_len, mask)

        # produce readable string encoded results
        stringout = stringOut(sort_targets, output)
        # Weights and collect outputs
        weight = None
        weight = model.backward_nlap(sort_feature, seq_len, mask)
        for i in range(weight.shape[0]):
            weights.append(weight[i])
            stringOuts.append(stringout[i])
        sort_seq_ids.extend(sort_chunk_ids)
        multiclass, binary, binary_total = calculate_accuracy(output, sort_targets)
        multiclass_correct += multiclass
        multiclass_instance += len(seq_len)
        binary_correct += binary
        binary_instance += binary_total

        # get gradient w.r.t. inputs here
        output.backward(torch.ones_like(output))
        grad_sa = (sort_feature.grad**2).sum(dim=-1)
        for i in range(weight.shape[0]):
            gradients.append(grad_sa[i])

    multi_accu = multiclass_correct*1.0/multiclass_instance
    binary_accu = binary_correct*1.0/binary_instance
    logger.info('Test Set Performance\tmulti_acc: {:0.3f} \tbinary_acc: {:0.3f} \t'.\
        format(multi_accu, binary_accu))
    print("Start analyzing ...")

    # weights: (b, l)
    test_sentence = [all_sentence[_id] for _id in sort_seq_ids]
    assert(len(test_sentence) == len(weights))

    word_level_w = dict()
    word_level_g = dict()
    word_level_c = dict()

    # save id to weights mapping for other plots
    id_weights = dict()
    id_labels = dict()
    id_gradients = dict()

    for i in range(len(test_sentence)):
        w = weights[i]
        g = gradients[i]
        s = test_sentence[i]
        w_r_pre = w[:len(s)].tolist()
        g_r_pre = g[:len(s)].tolist()

        w_r = None
        w_r = softmax(w_r_pre) 
        g_r = softmax(g_r_pre) 
        id_weights[sort_seq_ids[i]] = w_r.tolist()
        id_labels[sort_seq_ids[i]] = stringOuts[i]
        id_gradients[sort_seq_ids[i]] = g_r.tolist()

        # assign scores based on softmax results and sentence length
        for i in range(len(s)):
            w_w = w_r[i] * 1.0
            g_w = g_r[i] * 1.0
            w_s = s[i]
            if w_s not in word_level_w.keys():
                word_level_w[w_s] = [w_w]
                word_level_g[w_s] = [g_w]
            else:
                word_level_w[w_s].append(w_w)   
                word_level_g[w_s].append(g_w)      
            if w_s not in word_level_c.keys():
                word_level_c[w_s] = 1
            else:
                word_level_c[w_s] = word_level_c[w_s] + 1
    
    # save id to weight mapping
    pickle.dump( id_gradients, open( args.out_dir + "/id_gradients_test_sst.p", "wb") )
    pickle.dump( id_weights, open(args.out_dir + "/id_weights_test_sst.p", "wb") )
    pickle.dump( id_labels, open(args.out_dir + "/id_labels_test_sst.p", "wb") )

    # summarize and write to a file
    output_file = args.out_dir + "/words_Test_sst.csv"
    word_level_w = [(k, v) for k, v in word_level_w.items()] 

    with open(output_file, mode='w') as csv_file:
        file_writer = csv.writer(csv_file, delimiter=',')
        header = ["word", "count", "sum", "avg", "std", "sum_gs", "avg_gs", "std_gs"]
        file_writer.writerow(header)

        for kv in word_level_w:
            kv_gs = word_level_g[kv[0]]
            var = 0.0
            var_gs = 0.0
            if len(kv[1]) > 1:
                var = statistics.stdev(kv[1])
                var_gs = statistics.stdev(kv_gs)

            row = [kv[0], word_level_c[kv[0]], sum(kv[1]),
                   sum(kv[1])*1.0/word_level_c[kv[0]], var,
                   sum(kv_gs), sum(kv_gs)*1.0/word_level_c[kv[0]], var_gs]
            file_writer.writerow(row)

def main(args):
    if args.dataset == "SST":
        SST(args)
    elif args.dataset == "SEND":
        SEND(args)
    else:
        assert(False)
    
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--epochs', type=int, default=300, metavar='N',
                        help='number of epochs to train (default: 300)')
    parser.add_argument('--lr', type=float, default=1e-4, metavar='LR',
                        help='learning rate (default: 1e-4)')
    parser.add_argument('--data_dir', type=str, default="../../../SENDv1-data",
                        help='path to data base directory')
    parser.add_argument('--dataset', type=str, default="SEND",
                        help='the dataset we want to run (default: SEND)')
    parser.add_argument('--model_path', type=str, default="../df5f97d3/best-model.pth",
                        help='path to the saved model (end with .pth)')
    parser.add_argument('--out_dir', type=str, default="../save_lap/",
                        help='the directory to save all the results')
    args = parser.parse_args()
    main(args)
