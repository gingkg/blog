import math
from typing import List, Tuple

import numpy as np

import torch
import torch.nn as nn
import torch.optim as optim
from torch import Tensor
import torch.autograd as autograd
import torch.nn.functional as F
from torch.autograd import Variable
from torch.utils.data import Dataset, DataLoader
# plt.switch_backend('agg')

from rl_pytorch.TSP_dataset import TSPDataset, TSPUnlabeledDataset

USE_CUDA = False

# todo:
# 1. bidirectional True does not work
# 2. mask
# 3. plot
# 5. critic net
# 6. use_cuda gpu

def rnn_init(rnn_type: str, **kwargs) -> nn.RNNBase:
    if rnn_type in ["LSTM", "GRU", "RNN"]:
        rnn = getattr(nn, rnn_type)(**kwargs)
    return rnn

class Attention(nn.Module):
    use_tanh: bool
    C: int
    name: str

    def __init__(self, hidden_size, use_tanh=False, C=10, name='Bahdanau'):
        super(Attention, self).__init__()

        self.use_tanh = use_tanh
        self.C = C
        self.name = name

        if name == 'Bahdanau':
            self.W_query = nn.Linear(hidden_size, hidden_size)
            self.W_ref = nn.Conv1d(hidden_size, hidden_size, 1, 1)

            V = torch.FloatTensor(hidden_size)
            self.V = nn.Parameter(V)
            self.V.data.uniform_(-(1. / math.sqrt(hidden_size)), 1. / math.sqrt(hidden_size))

    def forward(self, query: Tensor, ref: Tensor) -> Tuple[Tensor, Tensor]:
        """
        Args:
            query: [batch_size * hidden_size]
            ref:   [batch_size * seq_len * hidden_size]
        Returns:
            ref:    [batch_size * hidden_size * seq_len]
            logits: [batch_size * seq_len]
        """

        batch_size = ref.size(0)
        seq_len = ref.size(1)

        if self.name == 'Bahdanau':
            ref = ref.permute(0, 2, 1)
            query = self.W_query(query).unsqueeze(2)  # [batch_size * hidden_size x 1]
            ref = self.W_ref(ref)  # [batch_size x hidden_size * seq_len]
            expanded_query = query.repeat(1, 1, seq_len)  # [batch_size * hidden_size * seq_len]
            V = self.V.unsqueeze(0).unsqueeze(0).repeat(batch_size, 1, 1)  # [batch_size * 1 * hidden_size]
            logits = torch.bmm(V, torch.tanh(expanded_query + ref)).squeeze(1)

        elif self.name == 'Dot':
            query = query.unsqueeze(2)
            logits = torch.bmm(ref, query).squeeze(2)  # [batch_size * seq_len x 1]
            ref = ref.permute(0, 2, 1)

        else:
            raise NotImplementedError

        if self.use_tanh:
            logits = self.C * torch.tanh(logits)
        else:
            logits = logits
        return ref, logits


class GraphEmbedding(nn.Module):
    embedding: nn.Parameter

    def __init__(self, input_size, embedding_size):
        super(GraphEmbedding, self).__init__()
        self.embedding = nn.Parameter(torch.FloatTensor(input_size, embedding_size))
        self.embedding.data.uniform_(-(1. / math.sqrt(embedding_size)), 1. / math.sqrt(embedding_size))

    def forward(self, batch_input: Tensor) -> Tensor:
        """

        Args:
            batch_input: [batch_size * 2 * seq_len]
        Returns:
            embedded: [batch_size * input_size * embedding_size]

        """
        batch_size = batch_input.size(0)
        seq_len = batch_input.size(2)
        embedding = self.embedding.repeat(batch_size, 1, 1)
        embedded = []
        batch_input = batch_input.unsqueeze(1)
        for i in range(seq_len):
            embedded.append(torch.bmm(batch_input[:, :, :, i].float(), embedding))
        embedded = torch.cat(embedded, 1)
        return embedded


# class Encoder(nn.Module):
#
#     def __init__(self, input_dim, hidden_dim):
#         super(Encoder, self).__init__()
#         self.hidden_dim = hidden_dim
#         self.lstm = nn.LSTM(input_dim, hidden_dim)
#         self.enc_init_state = self.init_hidden(hidden_dim)
#
#     def forward(self, x, hidden):
#         output, hidden = self.lstm(x, hidden)
#         return output, hidden
#
#     def init_hidden(self, hidden_dim):
#         enc_init_hidden = Variable(torch.zeros(hidden_dim), requires_grad=False)
#
#         enc_init_context = Variable(torch.zeros(hidden_dim), requires_grad=False)
#         return (enc_init_hidden, enc_init_context)


class Decoder(nn.Module):

    def apply_mask_to_logits(self, logits: Tensor, mask: Tensor, idxs: Tensor) -> Tuple[Tensor, Tensor]:
        """
        Args:
            logits: [batch_size * seq_len]
            mask:   [batch_size * seq_len]
            idxs:   None or tensor [batch_size]
        Returns:
            logits:      []
            mask_clone:  []
        """
        batch_size = logits.size(0)
        mask_clone = mask.clone()

        if idxs is not None:
            mask_clone[[i for i in range(batch_size)], idxs.data] = 1
            logits[mask_clone] = -np.inf
        return logits, mask_clone


class StochasticDecoder(Decoder):
    def __init__(self, embedding_size, hidden_size, num_glimpse, use_tanh, tanh_exploration):
        super(StochasticDecoder, self).__init__()

        self.embedding_size = embedding_size
        self.hidden_size = hidden_size
        self.num_glimpse = num_glimpse

        self.rnn = rnn_init('GRU', input_size = embedding_size, hidden_size=hidden_size, batch_first=True, bidirectional=False)
        self.decoder_start_input = nn.Parameter(torch.FloatTensor(embedding_size))
        self.decoder_start_input.data.uniform_(-(1. / math.sqrt(embedding_size)), 1. / math.sqrt(embedding_size))
        self.glimpse = Attention(hidden_size, use_tanh=False)
        self.pointer = Attention(hidden_size, use_tanh=use_tanh, C=tanh_exploration)


    def forward(self, decoder_input, batch_input, hidden, encoder_outputs):
        """
        Args:
            decoder_input: [batch_size x embedding_size]
            batch_input: [batch_size * seq_len * embedding_dim]
            hidden: the prev hidden state,[batch_size * hidden_size].
            encoder_outputs: [batch_size * seq_len * hidden_size]
        """
        batch_size = batch_input.size(0)
        seq_len = batch_input.size(1)
        prob_list = []
        action_idx_list = []
        # mask = torch.zeros(batch_size, seq_len).byte()
        mask = torch.zeros(batch_size, seq_len).bool()

        idxs = None

        for i in range(seq_len):
            _, hidden = self.rnn(decoder_input.unsqueeze(1), hidden)

            if isinstance(hidden, tuple):
                query = hidden[0].squeeze(0)
            else:
                query = hidden.squeeze(0)
            for i in range(self.num_glimpse):
                ref, logits = self.glimpse(query, encoder_outputs)
                logits, mask = self.apply_mask_to_logits(logits, mask, idxs)
                query = torch.bmm(ref, F.softmax(logits, dim=1).unsqueeze(2)).squeeze(2)

            _, logits = self.pointer(query, encoder_outputs)
            logits, mask = self.apply_mask_to_logits(logits, mask, idxs)
            probs = F.softmax(logits, dim=1)

            idxs = probs.multinomial(1).squeeze(1)  # [batch_size]
            for old_idxs in action_idx_list:
                if old_idxs.eq(idxs).data.any():
                    print(f'{seq_len} resample')
                    idxs = probs.multinomial(1).squeeze(1)
                    break
            decoder_input = batch_input[[i for i in range(batch_size)], idxs.data, :]  # [batch_size * embedded_size]

            prob_list.append(probs)
            action_idx_list.append(idxs)

        return prob_list, action_idx_list, hidden


class BeamDecoder(Decoder):
    def __init__(self, embedding_size, hidden_size, num_glimpse, use_tanh, tanh_exploration):
        super(BeamDecoder, self).__init__()

        self.embedding_size = embedding_size
        self.hidden_size = hidden_size
        self.num_glimpse = num_glimpse

        self.rnn = rnn_init('GRU', input_size = embedding_size, hidden_size=hidden_size, batch_first=True, bidirectional=False)
        self.decoder_start_input = nn.Parameter(torch.FloatTensor(embedding_size))
        self.decoder_start_input.data.uniform_(-(1. / math.sqrt(embedding_size)), 1. / math.sqrt(embedding_size))
        self.glimpse = Attention(hidden_size, use_tanh=False)
        self.pointer = Attention(hidden_size, use_tanh=use_tanh, C=tanh_exploration)


    def forward(self, decoder_input, batch_input, hidden, encoder_outputs):
        """
        Args:
            decoder_input: [batch_size x embedding_size]
            batch_input: [batch_size * seq_len * embedding_dim]
            hidden: the prev hidden state,[batch_size * hidden_size].
            encoder_outputs: [batch_size * seq_len * hidden_size]
        """
        batch_size = batch_input.size(0)
        seq_len = batch_input.size(1)
        prob_list = []
        action_idx_list = []
        # mask = torch.zeros(batch_size, seq_len).byte()
        mask = torch.zeros(batch_size, seq_len).bool()

        idxs = None

        for i in range(seq_len):
            pass

        return prob_list, action_idx_list, hidden


class PointerNet(nn.Module):
    use_embedding: bool
    embedding: GraphEmbedding
    num_glimpse: int
    encoder: nn.RNNBase
    decoder: Decoder
    pointer: Attention
    glimpse: Attention
    decoder_start_input: nn.Parameter

    def __init__(self, rnn_type, use_embedding, embedding_size, hidden_size, seq_len, num_glimpse, tanh_exploration, use_tanh, attention):
        super(PointerNet, self).__init__()

        self.use_embedding = use_embedding
        if use_embedding:
            self.embedding = GraphEmbedding(2, embedding_size)
        else:
            embedding_size = 2

        self.num_glimpse = num_glimpse
        self.encoder = rnn_init(rnn_type, input_size = embedding_size, hidden_size=hidden_size, batch_first=True, bidirectional=False)
        self.decoder = StochasticDecoder(embedding_size, hidden_size, num_glimpse=num_glimpse, use_tanh=use_tanh, tanh_exploration=tanh_exploration)
        self.decoder_start_input = nn.Parameter(torch.FloatTensor(embedding_size))
        self.decoder_start_input.data.uniform_(-(1. / math.sqrt(embedding_size)), 1. / math.sqrt(embedding_size))


    def forward(self, batch_input: Tensor) -> Tuple[List[Tensor], List[Tensor]]:
        """
        Args:
            batch_input: [batch_size * 2 * seq_len]
        Returns:
            prob_list:        [batch_size * seq_len][seq_len]
            action_idx_list:  [batch_size][seq_len]
        """
        batch_size = batch_input.size(0)
        seq_len = batch_input.size(2)

        if self.use_embedding:
            batch_input = self.embedding(batch_input)  # [batch_size * seq_len * embedded_size]
        else:
            batch_input = batch_input.permute(0, 2, 1)  # [batch_size * seq_len * embedded_size]

        encoder_outputs, hidden = self.encoder(batch_input)
        decoder_input = self.decoder_start_input.unsqueeze(0).repeat(batch_size, 1)
        pointer_probs, input_idxs, dec_hidden_t = self.decoder(decoder_input, batch_input, hidden, encoder_outputs)
        return pointer_probs, input_idxs


class CombinatorialRL(nn.Module):
    actor: PointerNet

    def __init__(self, rnn_type, use_embedding, embedding_size, hidden_size, seq_len, num_glimpse, tanh_exploration, use_tanh, attention):
        super(CombinatorialRL, self).__init__()

        self.actor = PointerNet(rnn_type, use_embedding, embedding_size, hidden_size, seq_len, num_glimpse, tanh_exploration, use_tanh, attention)

    def forward(self, batch_input: Tensor) -> Tuple[Tensor, List[Tensor], List[Tensor], List[Tensor]]:
        """
        Args:
            batch_input: [batch_size * 2 * seq_len]
        Returns:
            R: Tensor of shape [batch_size]
            action_prob_list: List of [seq_len], tensor shape [batch_size]
            action_list:      List of [seq_len], tensor shape [batch_size * 2]
            action_idx_list:  List of [seq_len], tensor shape [batch_size]
        """
        batch_size = batch_input.size(0)
        seq_len = batch_input.size(2)
        prob_list, action_idx_list = self.actor(batch_input)

        action_list = []
        batch_input = batch_input.transpose(1, 2)
        for action_id in action_idx_list:
            action_list.append(batch_input[[x for x in range(batch_size)], action_id.data, :])
        action_prob_list = []
        for prob, action_id in zip(prob_list, action_idx_list):
            action_prob_list.append(prob[[x for x in range(batch_size)], action_id.data])

        R = self.reward(action_list)

        return R, action_prob_list, action_list, action_idx_list


    def reward(self, sample_solution: List[Tensor]) -> Tensor:
        """
        Computes total distance of tour
        Args:
            sample_solution: list of size N, each tensor of shape [batch_size * 2]

        Returns:
            tour_len: [batch_size]

        """
        batch_size = sample_solution[0].size(0)
        n = len(sample_solution)
        tour_len = Variable(torch.zeros([batch_size]))

        for i in range(n - 1):
            tour_len += torch.norm(sample_solution[i] - sample_solution[i + 1], dim=1)
        tour_len += torch.norm(sample_solution[n - 1] - sample_solution[0], dim=1)
        return tour_len



if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser("TSP_RL")
    parser.add_argument("--gpu", type=int, default=0)
    parser.add_argument("--batch_size", type=int, default=32)
    parser.add_argument("--hidden_size", type=int, default=128)
    parser.add_argument("--random_train_size", type=int, default=100000)
    parser.add_argument("--random_validate_size", type=int, default=1000)
    parser.add_argument("--num_glimpse", type=int, default=1)
    parser.add_argument("--use_embedding", type=int, default=1)
    parser.add_argument("--embedding_size", type=int, default=128)
    parser.add_argument("--num_epoch", type=int, default=5)
    parser.add_argument("--train_filename", type=str, default="../tsp_10_test_sample.txt")
    parser.add_argument("--validate_filename", type=str, default="../tsp_10_test_sample.txt")
    parser.add_argument("--clip_norm", type=float, default=2.)
    parser.add_argument("--threshold", type=float, default=3.99)
    parser.add_argument("--rnn_type", type=str, default='GRU')
    parser.add_argument("--log_dir", type=str, default="./log")

    args = parser.parse_args()

    tanh_exploration = 10
    use_tanh = True
    beta = 0.9

    RL_model = CombinatorialRL(args.rnn_type, args.use_embedding, args.embedding_size, args.hidden_size, 10, args.num_glimpse, tanh_exploration, use_tanh, attention="Dot")

    use_random_ds = True
    if use_random_ds:
        train_dataset = TSPUnlabeledDataset(10, args.random_train_size)
        validate_dataset = TSPUnlabeledDataset(10, args.random_validate_size)
        train_loader = DataLoader(train_dataset, batch_size=args.batch_size, shuffle=True, num_workers=0)
        validate_loader = DataLoader(validate_dataset, batch_size=args.batch_size, shuffle=True, num_workers=0)
    else:
        train_ds = TSPDataset(args.train_filename, 10, 10)
        test_ds = TSPDataset(args.validate_file, 10, 10)
        train_loader = DataLoader(train_ds, num_workers=0, batch_size=args.batch_size)
        validate_loader = DataLoader(test_ds, num_workers=0, batch_size=args.batch_size)

    actor_optim = optim.Adam(RL_model.actor.parameters(), lr=1e-4)
    critic_exp_mvg_avg = torch.zeros(1)
    threshold_stop = False

    for epoch in range(args.num_epoch):
        batch_id = 0
        for batch_item in train_loader:
            batch_input = batch_item[0] # [batch_size * 2 * seq_len]
            batch_id += 1
            train_tour = []
            print(f'{epoch}: {batch_id}')
            RL_model.train()

            batch_input = Variable(batch_input)

            R, prob_list, action_list, actions_idx_list = RL_model(batch_input)

            if batch_id == 0:
                critic_exp_mvg_avg = R.mean()
            else:
                critic_exp_mvg_avg = (critic_exp_mvg_avg * beta) + ((1. - beta) * R.mean())

            advantage = R - critic_exp_mvg_avg

            log_probs = 0
            for prob in prob_list:
                log_prob = torch.log(prob)
                log_probs += log_prob
            log_probs[log_probs < -1000] = 0.

            reinforce = advantage * log_probs
            actor_loss = reinforce.mean()

            actor_optim.zero_grad()
            actor_loss.backward()
            torch.nn.utils.clip_grad_norm_(RL_model.actor.parameters(), float(args.clip_norm), norm_type=2)

            actor_optim.step()

            critic_exp_mvg_avg = critic_exp_mvg_avg.detach()

            train_tour.append(R.mean().item())

            if batch_id % 100 == 0:
                validate_tour = []
                RL_model.eval()
                for validate_batch in validate_loader:
                    batch_input_validate = Variable(validate_batch)
                    R, prob_list, action_list, actions_idx_list = RL_model(batch_input_validate)
                    validate_tour.append(R.mean().item())

                validate_tour_avg_r = sum(validate_tour) / len(validate_tour)
                train_tour_batch_avg_r = sum(train_tour) / len(train_tour)
                print(f'{epoch} : {batch_id}')
                print(f'validate tour {validate_tour_avg_r}')
                print(f'train tour {train_tour_batch_avg_r}')

                if args.threshold and validate_tour_avg_r < args.threshold:
                    threshold_stop = True
                    print("EARLY STOP!")
                    break
            if threshold_stop:
                break;


