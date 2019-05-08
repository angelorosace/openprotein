# This file is part of the TMHMM3 project.
#
# @author Jeppe Hallgren
#
# For license information, please see the LICENSE file in the root directory.

import torch
import torch.autograd as autograd
import torch.nn as nn
from pytorchcrf.torchcrf import CRF
import time

import openprotein
from tm_util import *
from sklearn.ensemble import RandomForestClassifier

# seed random generator for reproducibility
torch.manual_seed(1)

class TMHMM3(openprotein.BaseModel):
    def __init__(self, num_labels, embedding, hidden_size, use_gpu, use_hmm_model, use_marg_prob, allowed_transitions):
        super(TMHMM3, self).__init__(embedding, use_gpu)

        # initialize model variables
        num_tags = num_labels + (2 * 40 if use_hmm_model else 0)
        self.hidden_size = hidden_size
        self.use_gpu = use_gpu
        self.use_marg_prob = use_marg_prob
        self.use_hmm_model = use_hmm_model
        self.embedding = embedding
        self.embedding_function = nn.Embedding(24, self.get_embedding_size())
        self.bi_lstm = nn.LSTM(self.get_embedding_size(), self.hidden_size, num_layers=1, bidirectional=True)
        self.hidden_to_labels = nn.Linear(self.hidden_size * 2, num_labels) # * 2 for bidirectional
        self.allowed_transitions = allowed_transitions
        self.crfModel = CRF(num_tags)
        self.type_classier = None
        self.type_tm_classier = None
        self.type_sp_classier = None
        self.ctc_loss = nn.CTCLoss()
        crf_transitions_mask = torch.ones((num_tags, num_tags)).byte()

        # if on GPU, move state to GPU memory
        if self.use_gpu:
            self.embedding_function = self.embedding_function.cuda()
            self.crfModel = self.crfModel.cuda()
            self.bi_lstm = self.bi_lstm.cuda()
            self.hidden_to_labels = self.hidden_to_labels.cuda()
            crf_transitions_mask = crf_transitions_mask.cuda()

        # compute mask matrix from allow transitions list
        for i in range(num_tags):
            for k in range(num_tags):
                if (i, k) in self.allowed_transitions:
                    crf_transitions_mask[i][k] = 0

        # generate masked transition parameters
        crf_start_transitions, crf_end_transitions, crf_transitions = \
            self.generate_masked_crf_transitions(
                self.crfModel, (None, crf_transitions_mask, None)
            )

        # initialize CRF
        self.initialize_crf_parameters(self.crfModel,
                                       start_transitions=crf_start_transitions,
                                       end_transitions=crf_end_transitions,
                                       transitions=crf_transitions)

    def initialize_crf_parameters(self,
                                  crfModel,
                                  start_transitions=None,
                                  end_transitions=None,
                                  transitions=None) -> None:
        """Initialize the transition parameters.

        The parameters will be initialized randomly from a uniform distribution
        between -0.1 and 0.1, unless given explicitly as an argument.
        """
        if start_transitions is None:
            nn.init.uniform(crfModel.start_transitions, -0.1, 0.1)
        else:
            crfModel.start_transitions.data = start_transitions
        if end_transitions is None:
            nn.init.uniform(crfModel.end_transitions, -0.1, 0.1)
        else:
            crfModel.end_transitions.data = end_transitions
        if transitions is None:
            nn.init.uniform(crfModel.transitions, -0.1, 0.1)
        else:
            crfModel.transitions.data = transitions

    def generate_masked_crf_transitions(self, crf_model, transition_mask):
        start_transitions_mask, transitions_mask, end_transition_mask = transition_mask
        start_transitions = crf_model.start_transitions.data.clone()
        end_transitions = crf_model.end_transitions.data.clone()
        transitions = crf_model.transitions.data.clone()
        if start_transitions_mask is not None:
            start_transitions.masked_fill_(start_transitions_mask, -100000000)
        if end_transition_mask is not None:
            end_transitions.masked_fill_(end_transition_mask, -100000000)
        if transitions_mask is not None:
            transitions.masked_fill_(transitions_mask, -100000000)
        return start_transitions, end_transitions, transitions

    def get_embedding_size(self):
        return 24 # bloom matrix has size 24

    def flatten_parameters(self):
        self.bi_lstm.flatten_parameters()

    def encode_amino_acid(self, letter):
        if self.embedding == "BLOSUM62":
            # blosum encoding
            if not globals().get('blosum_encoder'):
                blosum_matrix = np.loadtxt("data/blosum62.csv", delimiter=",")
                blosum_key = "A,R,N,D,C,Q,E,G,H,I,L,K,M,F,P,S,T,W,Y,V,B,Z,X,U".split(",")
                key_map = {}
                for idx, value in enumerate(blosum_key):
                    key_map[value] = list([int(v) for v in blosum_matrix[idx].astype('int')])
                globals().__setitem__("blosum_encoder", key_map)
            return globals().get('blosum_encoder')[letter]
        elif self.embedding == "ONEHOT":
            # one hot encoding
            one_hot_key = "A,R,N,D,C,Q,E,G,H,I,L,K,M,F,P,S,T,W,Y,V,B,Z,X,U".split(",")
            arr = []
            for idx, k in enumerate(one_hot_key):
                if k == letter:
                    arr.append(1)
                else:
                    arr.append(0)
            return arr
        elif self.embedding == "PYTORCH":
            key_id = "A,R,N,D,C,Q,E,G,H,I,L,K,M,F,P,S,T,W,Y,V,B,Z,X,U".split(",")
            for idx, k in enumerate(key_id):
                if k == letter:
                    return idx

    def embed(self, prot_aa_list):
        embed_list = []
        for aa_list in prot_aa_list:
            t = list([self.encode_amino_acid(aa) for aa in aa_list])
            if self.embedding == "PYTORCH":
                t = torch.LongTensor(t)
            else:
                t= torch.FloatTensor(t)
            if self.use_gpu:
                t = t.cuda()
            embed_list.append(t)
        return embed_list

    def init_hidden(self, minibatch_size):
        # number of layers (* 2 since bidirectional), minibatch_size, hidden size
        initial_hidden_state = torch.zeros(1 * 2, minibatch_size, self.hidden_size)
        initial_cell_state = torch.zeros(1 * 2, minibatch_size, self.hidden_size)
        if self.use_gpu:
            initial_hidden_state = initial_hidden_state.cuda()
            initial_cell_state = initial_cell_state.cuda()
        self.hidden_layer = (autograd.Variable(initial_hidden_state),
                             autograd.Variable(initial_cell_state))

    def _get_network_emissions(self, input_sequences):

        if self.embedding == "PYTORCH":
            pad_seq, seq_length = torch.nn.utils.rnn.pad_sequence(input_sequences), [v.size(0) for v in input_sequences]
            pad_seq_embed = self.embedding_function(pad_seq)
            packed = torch.nn.utils.rnn.pack_padded_sequence(pad_seq_embed, seq_length)
        else:
            packed = torch.nn.utils.rnn.pack_sequence(input_sequences)
        minibatch_size = len(input_sequences)
        self.init_hidden(minibatch_size)
        bi_lstm_out, self.hidden_layer = self.bi_lstm(packed, self.hidden_layer)
        data, batch_sizes = bi_lstm_out
        emissions = self.hidden_to_labels(data)
        if self.use_hmm_model:
            inout_select = torch.LongTensor([0])
            outin_select = torch.LongTensor([1])
            if self.use_gpu:
                inout_select = inout_select.cuda()
                outin_select = outin_select.cuda()
            inout = torch.index_select(emissions, 1, autograd.Variable(inout_select))
            outin = torch.index_select(emissions, 1, autograd.Variable(outin_select))
            emissions = torch.cat((emissions, inout.expand(-1, 40), outin.expand(-1, 40)), 1)
        emissions_padded = torch.nn.utils.rnn.pad_packed_sequence(torch.nn.utils.rnn.PackedSequence(emissions,batch_sizes))
        return emissions_padded

    def batch_sizes_to_mask(self, batch_sizes):
        mask = torch.autograd.Variable(torch.t(torch.ByteTensor(
            [[1] * int(batch_size) + [0] * (int(batch_sizes[0]) - int(batch_size)) for batch_size in batch_sizes]
        )))
        if self.use_gpu:
            mask = mask.cuda()
        return mask

    def get_last_reduced_mean(self):
        return self.last_reduced_mean

    def compute_loss(self, training_minibatch):
        _, labels_list, remapped_labels_list, prot_type_list, prot_name_list, original_aa_string = training_minibatch
        minibatch_size = len(labels_list)
        labels_to_use = remapped_labels_list if self.use_hmm_model else labels_list
        if False:
            actual_labels = torch.nn.utils.rnn.pad_sequence([autograd.Variable(l) for l in labels_to_use])

            input_sequences = [autograd.Variable(x) for x in self.embed(original_aa_string)]
            emissions, batch_sizes = self._get_network_emissions(input_sequences)
            loss = -1 * self.crfModel(emissions, actual_labels, mask=self.batch_sizes_to_mask(batch_sizes))
            if float(loss) > 100000:
                for idx, batch_size in enumerate(batch_sizes):
                    last_label = None
                    for i in range(batch_size):
                        label = int(actual_labels[i][idx])
                        write_out(str(label) + ",", end='')
                        if last_label is not None and (last_label, label) not in self.allowed_transitions:
                            write_out("Error: invalid transition found")
                            write_out((last_label, label))
                            exit()
                        last_label = label
                    write_out(" ")
            return loss / minibatch_size
        else:
            # CTC loss
            input_sequences = [autograd.Variable(x) for x in self.embed(original_aa_string)]
            emissions, batch_sizes = self._get_network_emissions(input_sequences)
            blank = torch.ones((emissions.size()[0], emissions.size()[1], 1)).cuda() * 1e-20
            em2 = torch.cat((blank, emissions), dim=2)
            output = torch.nn.functional.log_softmax(em2, dim=2)
            topologies = list([torch.stack(list([label for (idx, label) in label_list_to_topology(a+1)])) for a in labels_list])


            targets, target_lengths = torch.nn.utils.rnn.pad_sequence(topologies).transpose(0,1), list([a.size()[0] for a in topologies])
            return self.ctc_loss(output, targets, tuple(batch_sizes), tuple(target_lengths))

    def calculate_margin_probabilities(self, input_sequences):
        print("Calculating marginal probabilities on minibatch")
        emissions, batch_sizes = self._get_network_emissions(input_sequences)
        mask = self.batch_sizes_to_mask(batch_sizes)
        s = torch.nn.Softmax(dim=2)
        marginal_probabilities = s(autograd.Variable(self.crfModel.compute_log_marginal_probabilities(emissions.data.clone(), mask.data.clone())))
        if marginal_probabilities.shape[2] is not 5:
            marginal_probabilities[:, :, 0] = torch.sum(marginal_probabilities[:, :, 5:45], dim=2)
            marginal_probabilities[:, :, 1] = torch.sum(marginal_probabilities[:, :, 45:85], dim=2)
            marginal_probabilities = marginal_probabilities[:, :, :5]
        probs_reduced_prefix = torch.mean(marginal_probabilities[:50, :, 2], dim=0)
        probs_reduced = torch.mean(marginal_probabilities, dim=0)
        probs_reduced[:, 2] = probs_reduced_prefix
        return probs_reduced.data

    def train_type_predictor(self, train_set, minibatch_size):
        if self.use_marg_prob:
            print("Using marginal probabilities...")
            train_dataloader = contruct_dataloader_from_disk(train_set, minibatch_size, balance_classes=True)
            marginal_probabilities = []
            actual_types = []
            for i, data in enumerate(train_dataloader, 0):
                print("Computing margin probs on minibatch",i)
                aa_list_sorted, labels_list, remapped_labels_list, prot_type_list, prot_name_list, original_aa_string = data
                actual_types.extend(list(map(int,prot_type_list)))
                marginal_probabilities.append(self.calculate_margin_probabilities([autograd.Variable(x) for x in self.embed(original_aa_string)]))
            marginal_probabilities = torch.cat(marginal_probabilities, dim = 0)
            print("Training random forest...")

            clf_tm = RandomForestClassifier(max_depth=6, random_state=0)
            clf_sp = RandomForestClassifier(max_depth=6, random_state=0)
            clf_tm.fit(marginal_probabilities.cpu(), list(map(is_tm,actual_types)))
            clf_sp.fit(marginal_probabilities.cpu(), list(map(is_sp,actual_types)))
            self.type_classier_tm = clf_tm
            self.type_classier_sp = clf_sp

    def forward(self, original_aa_string, forced_types=None):
        input_sequences = [autograd.Variable(x) for x in self.embed(original_aa_string)]
        emissions, batch_sizes = self._get_network_emissions(input_sequences)
        mask = self.batch_sizes_to_mask(batch_sizes)
        labels_remapped = self.crfModel.decode(emissions, mask=mask)
        predicted_labels = list(map(remapped_labels_to_orginal_labels, labels_remapped))
        if self.use_marg_prob:
            marginal_probabilities = self.calculate_margin_probabilities(input_sequences).cpu()
            predicted_types_tm = list(map(int, self.type_classier_tm.predict(marginal_probabilities)))
            predicted_types_sp = list(map(int, self.type_classier_sp.predict(marginal_probabilities)))
            predicted_types = list(map(get_type_from_tm_sp,zip(predicted_types_tm, predicted_types_sp)))
        else:
            predicted_types = list(map(get_predicted_type_from_labels, predicted_labels))
        return predicted_labels, predicted_types if forced_types is None else forced_types

    def evaluate_model(self, data_loader):
        validation_loss_tracker = []
        for i, minibatch in enumerate(data_loader, 0):
            validation_loss_tracker.append(self.compute_loss(minibatch).detach())
        loss = torch.stack(validation_loss_tracker).mean()

        data = {}

        return float(loss), data


def is_sp(type_id):
    if type_id == 0 or type_id == 3:
        return 0
    else:
        return 1

def is_tm(type_id):
    if type_id == 0 or type_id == 1:
        return 1
    else:
        return 0

def get_type_from_tm_sp(t):
    is_tm, is_sp = t
    if is_tm ==  1:
        if is_sp == 1:
            return 1
        else:
            return 0
    else:
        if is_sp == 1:
            return 2
        else:
            return 3