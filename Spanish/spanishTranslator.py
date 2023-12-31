"""
Translation with a Sequence to Sequence Network and Attention
****************************************************************
Author: Eddie Gomez
Inspired by Sean Robertson <https://github.com/spro>`_
"""
import numpy as np
from io import open
import unicodedata
import string
import re
import random

import torch
import torch.nn as nn
from torch import optim
import torch.nn.functional as F

# I assume cpu for me
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")



from torch.optim import Adam
#https://torchkge.readthedocs.io/en/latest/
from torchkge.models.bilinear import ComplExModel
from torchkge.models.deep import ConvKBModel
from torchkge.sampling import BernoulliNegativeSampler
from torchkge.utils import MarginLoss, DataLoader
from tqdm.autonotebook import tqdm
import pandas as pd

###################################################################
#
# Making connection to our neo4j database via py2neo
#
#
#https://medium.com/stanford-cs224w/simple-schemes-for-knowledge-graph-embedding-dd07c61f3267

from py2neo import Graph
from neo4j import GraphDatabase
# Connect to the Neo4j database
# Remember to change password to your own when running the program. 
graph = Graph("bolt://localhost:7687", user="neo4j", password="password")

# Define the Cypher query to retrieve all data
cypher_query = """
MATCH (e:English)-[r:TRANSLATES_TO]->(s:Spanish)
RETURN s.word AS spanish_word, e.word AS english_word, TYPE(r) AS relationship
"""
## Execute the Cypher query and retrieve the results
results = graph.run(cypher_query).data()
#
head1 = [row['spanish_word'] for row in results]
relation1 = [row['relationship'] for row in results]
tail1 = [row['english_word'] for row in results]


# Print the retrieved data
count = 1
for result in results:
    print(count, " ", result['spanish_word'], " ", result['relationship'], " ", result['english_word'])
    count= count + 1



entities = [word for pair in zip(head1, tail1) for word in pair]

relations = relation1

#enumerating for our knowledge embeddings
entity2idx = {entity: idx for idx, entity in enumerate(entities)}
relation2idx = {relation1: idx for idx, relation1 in enumerate(relations)}

# Convert data to numerical form
train_data = [(entity2idx[h], relation2idx[r], entity2idx[t]) for h, r, t in zip(head1, relation1, tail1)]

## Read into Torchkg for our knowledge embedding
# TransE Model
class TransE(nn.Module):
    def __init__(self, num_entities, num_relations, embedding_dim):
        super(TransE, self).__init__()
        self.entity_embeddings = nn.Embedding(num_entities, embedding_dim)
        self.relation_embeddings = nn.Embedding(num_relations, embedding_dim)

    def forward(self, head, relation, tail):
        h = self.entity_embeddings(head)
        r = self.relation_embeddings(relation)
        t = self.entity_embeddings(tail)
        score = torch.norm(h + r - t, p=2, dim=1)
        return score
    
# Instantiate the model and define the loss function
num_entities = len(entities)
num_relations = len(relations)
embedding_dim = 50

model = TransE(num_entities, num_relations, embedding_dim)
criterion = nn.MarginRankingLoss(margin=1.0)
optimizer = optim.Adam(model.parameters(), lr=0.01)




# Training loop
num_epochs = 10

for epoch in range(num_epochs):
    total_loss = 0.0
    np.random.shuffle(train_data)

    for head, relation, tail in train_data:
        optimizer.zero_grad()

        positive_score = model.forward(torch.LongTensor([head]), torch.LongTensor([relation]), torch.LongTensor([tail]))

        corrupted_tail = np.random.choice(num_entities, size=1)[0]
        negative_score = model.forward(torch.LongTensor([head]), torch.LongTensor([relation]), torch.LongTensor([corrupted_tail]))

        target = torch.tensor([-1])  # Negative target score
        loss = criterion(positive_score, negative_score, target)
        total_loss += loss.item()
        loss.backward()
        optimizer.step()

    if (epoch + 1) % 10 == 0:
        print(f"Epoch {epoch+1}/{num_epochs}, Loss: {total_loss:.4f}")

# Step 5: Retrieve the learned embeddings
entity_embeddings = model.entity_embeddings.weight.data.numpy()

# Print the learned embeddings
for entity, idx in entity2idx.items():
    print(idx, entity)
print("----------------------")
while True:
    user_input = input("Enter a word in Spanish (or 'exit' to quit): ")
    if user_input.lower() == "exit":
        break

    # Convert user input to numerical form
    if user_input in entity2idx:
        spanish_idx = entity2idx[user_input]
        spanish_tensor = torch.LongTensor([spanish_idx])

        # Retrieve the embedding for the user input
        spanish_embedding = model.entity_embeddings(spanish_tensor)

        # Calculate distances between the user input embedding and all English entity embeddings
        distances = torch.norm(spanish_embedding - model.entity_embeddings.weight.data, p=2, dim=1)
        # Exclude the input Spanish word from consideration by setting its distance to a large value
        distances[spanish_idx] = float('inf')

        # Find the index of the closest English embedding
        closest_idx = torch.argmin(distances)

        # Retrieve the corresponding English word from entity2idx dictionary
        english_word = list(entity2idx.keys())[list(entity2idx.values()).index(closest_idx)]

        print(f"The corresponding English word for '{user_input}' is '{english_word}'.")
    else:
        print("Word not found in the vocabulary.")
#torchKGData = KnowledgeGraph(df=data)
######################################################################
# Unique index per word to use as the inputs and targets of
# the networks. To keep track we use class Lang
# Lang does word → index (``word2index``) and index → word
# (``index2word``) dictionaries, as well as a count of each word
# ``word2count`` which will be used to replace rare words later.
# SOS: startOfSentence EOS: endOfSentence

SOS_token = 0
EOS_token = 1

class Lang:
    def __init__(self, name):
        self.name = name
        self.word2index = {}
        self.word2count = {}
        self.index2word = {0: "SOS", 1: "EOS"}
        self.n_words = 2  # Count SOS and EOS

    def addSentence(self, sentence):
        for word in sentence.split(' '):
            self.addWord(word)

    def addWord(self, word):
        if word not in self.word2index:
            self.word2index[word] = self.n_words
            self.word2count[word] = 1 # augmenting this so we get rare words
            self.index2word[self.n_words] = word
            self.n_words += 1
        else:
            self.word2count[word] += 1


######################################################################
# The files are all in Unicode, to simplify we will turn Unicode
# characters to ASCII, make everything lowercase, and trim most
# punctuation.
# Turn a Unicode string to plain ASCII, thanks to
# https://stackoverflow.com/a/518232/2809427
def unicodeToAscii(s):
    return ''.join(
        c for c in unicodedata.normalize('NFD', s)
        if unicodedata.category(c) != 'Mn'
    )
# Lowercase, trim, and remove non-letter characters
def normalizeString(s):
    s = unicodeToAscii(s.lower().strip())
    s = re.sub(r"([.!?])", r" \1", s)
    s = re.sub(r"[^a-zA-Z.!?]+", r" ", s)
    return s

######################################################################
# THIS CALLS LANG. Ithis splits sentences and sends them to lang to get them enumerated
# To read the data file we will split the file into lines, and then split
# lines into pairs. The files are all English → Other Language, so if we
# want to translate from Other Language → English I added the ``reverse``
# flag to reverse the pairs.
#

def readLangs(lang1, lang2, reverse=False):
    print("Reading lines...")

    # Read the file and split into lines

    # Using readlines()
    file1 = open('spavshort.txt', 'r', encoding="utf8")
    lines = file1.readlines()
    count = 0
    # Strips the newline character
    for line in lines:
        line.strip()
        head, sep, tail = line.partition('\tCC-BY')
        lines[count] = head    
        count += 1

    # Split every line into pairs and normalize
    pairs = [[normalizeString(s) for s in l.split('\t')] for l in lines]
    # Reverse pairs, make Lang instances
    if reverse:
        pairs = [list(reversed(p)) for p in pairs]
        input_lang = Lang(lang2)
        output_lang = Lang(lang1)
    else:
        input_lang = Lang(lang1)
        output_lang = Lang(lang2)

    return input_lang, output_lang, pairs


######################################################################
# Since there are a *lot* of example sentences and we want to train
# something quickly, we'll trim the data set to only relatively short and
# simple sentences. Here the maximum length is 10 words (that includes
# ending punctuation) and we're filtering to sentences that translate to
# the form "I am" or "He is" etc. (accounting for apostrophes replaced
# earlier).
# TRIM FOR EASIER SENTENCES TO DO LESS WORK
MAX_LENGTH = 10

#eng_prefixes = (
#    "i am ", "i m ",
#    "he is", "he s ",
#    "she is", "she s ",
#    "you are", "you re ",
#    "we are", "we re ",
#    "they are", "they re "
#)
def filterPair(p):
    return len(p[0].split(' ')) < MAX_LENGTH and \
        len(p[1].split(' ')) < MAX_LENGTH# and \
       #p[1].startswith(eng_prefixes)

def filterPairs(pairs):
    return [pair for pair in pairs if filterPair(pair)]

######################################################################
# The full process to prepare data is called below. 
#
# -  Read text file and split into lines, split lines into pairs
# -  Normalize text, filter by length and content
# -  Make word lists from sentences in pairs
#

def prepareData(lang1, lang2, reverse=False):
    input_lang, output_lang, pairs = readLangs(lang1, lang2, reverse)
    print("Read %s sentence pairs" % len(pairs))
    pairs = filterPairs(pairs)
    print("Trimmed to %s sentence pairs" % len(pairs))
    print("Counting words...")
    for pair in pairs:
        input_lang.addSentence(pair[0])
        output_lang.addSentence(pair[1])
    print("Counted words:")
    print(input_lang.name, input_lang.n_words)
    print(output_lang.name, output_lang.n_words)
    return input_lang, output_lang, pairs

# We print a random pair. I might try printing all pairs, we'll see. 
input_lang, output_lang, pairs = prepareData('eng', 'spa', True)
print(random.choice(pairs))


######################################################################
# The Encoder
# -----------
#
# The encoder of a seq2seq network is a RNN that outputs some value for
# every word from the input sentence. For every input word the encoder
# outputs a vector and a hidden state, and uses the hidden state for the
# next input word.
#
# .. figure:: /_static/img/seq-seq-images/encoder-network.png
#    :alt:
#
# sizes

class EncoderRNN(nn.Module):
    def __init__(self, input_size, hidden_size):
        print("Encoder ")
        print(input_size, hidden_size)
        super(EncoderRNN, self).__init__()
        self.hidden_size = hidden_size

        self.embedding = nn.Embedding(input_size, hidden_size)
        self.gru = nn.GRU(hidden_size, hidden_size)

    def forward(self, input, hidden):
        embedded = self.embedding(input).view(1, 1, -1)
        output = embedded
        output, hidden = self.gru(output, hidden)
        return output, hidden

    def initHidden(self):
        return torch.zeros(1, 1, self.hidden_size, device=device)

######################################################################
# The Decoder
# -----------
#
# The decoder is another RNN that takes the encoder output vector(s) and
# outputs a sequence of words to create the translation.
#


######################################################################
# Simple Decoder
# ^^^^^^^^^^^^^^
#
# In the simplest seq2seq decoder we use only last output of the encoder.
# This last output is sometimes called the *context vector* as it encodes
# context from the entire sequence. This context vector is used as the
# initial hidden state of the decoder.
#
# At every step of decoding, the decoder is given an input token and
# hidden state. The initial input token is the start-of-string ``<SOS>``
# token, and the first hidden state is the context vector (the encoder's
# last hidden state).
#
# .. figure:: /_static/img/seq-seq-images/decoder-network.png
#    :alt:
#
#

class DecoderRNN(nn.Module):
    def __init__(self, hidden_size, output_size):
        print("Decoder ")
        print(hidden_size, output_size)
        super(DecoderRNN, self).__init__()
        self.hidden_size = hidden_size

        self.embedding = nn.Embedding(output_size, hidden_size)
        self.gru = nn.GRU(hidden_size, hidden_size)
        self.out = nn.Linear(hidden_size, output_size)
        self.softmax = nn.LogSoftmax(dim=1)

    def forward(self, input, hidden):
        output = self.embedding(input).view(1, 1, -1)
        output = F.relu(output)
        output, hidden = self.gru(output, hidden)
        output = self.softmax(self.out(output[0]))
        return output, hidden

    def initHidden(self):
        return torch.zeros(1, 1, self.hidden_size, device=device)

######################################################################
# I encourage you to train and observe the results of this model, but to
# save space we'll be going straight for the gold and introducing the
# Attention Mechanism.
#


######################################################################
# Attention Decoder
# ^^^^^^^^^^^^^^^^^
#
# If only the context vector is passed between the encoder and decoder,
# that single vector carries the burden of encoding the entire sentence.
#
# Attention allows the decoder network to "focus" on a different part of
# the encoder's outputs for every step of the decoder's own outputs. First
# we calculate a set of *attention weights*. These will be multiplied by
# the encoder output vectors to create a weighted combination. The result
# (called ``attn_applied`` in the code) should contain information about
# that specific part of the input sequence, and thus help the decoder
# choose the right output words.
#
# .. figure:: https://i.imgur.com/1152PYf.png
#    :alt:
#
# Calculating the attention weights is done with another feed-forward
# layer ``attn``, using the decoder's input and hidden state as inputs.
# Because there are sentences of all sizes in the training data, to
# actually create and train this layer we have to choose a maximum
# sentence length (input length, for encoder outputs) that it can apply
# to. Sentences of the maximum length will use all the attention weights,
# while shorter sentences will only use the first few.
#
# .. figure:: /_static/img/seq-seq-images/attention-decoder-network.png
#    :alt:
#
# sizes

class AttnDecoderRNN(nn.Module):
    def __init__(self, hidden_size, output_size, dropout_p=0.1, max_length=MAX_LENGTH):
        print("Att Decoder ")
        print(hidden_size, output_size)
        
        super(AttnDecoderRNN, self).__init__()
        self.hidden_size = hidden_size
        self.output_size = output_size
        self.dropout_p = dropout_p
        self.max_length = max_length

        self.embedding = nn.Embedding(self.output_size, self.hidden_size)
        self.fc_hidden = nn.Linear(self.hidden_size, self.hidden_size, bias=False)
        self.fc_encoder = nn.Linear(self.hidden_size, self.hidden_size, bias=False)
        self.alignment_vector = nn.Parameter(torch.Tensor(1, hidden_size))
        torch.nn.init.xavier_uniform_(self.alignment_vector)
        self.dropout = nn.Dropout(self.dropout_p)
        self.gru = nn.GRU(self.hidden_size * 2, self.hidden_size)
        self.out = nn.Linear(self.hidden_size, self.output_size)

    def forward(self, input, hidden, encoder_outputs):
        embedded = self.embedding(input).view(1, -1)
        embedded = self.dropout(embedded)

        transformed_hidden = self.fc_hidden(hidden[0])
        expanded_hidden_state = transformed_hidden.expand(self.max_length, -1)
        alignment_scores = torch.tanh(expanded_hidden_state +
                                      self.fc_encoder(encoder_outputs))
        alignment_scores = self.alignment_vector.mm(alignment_scores.T)
        attn_weights = F.softmax(alignment_scores, dim=1)
        context_vector = attn_weights.mm(encoder_outputs)

        output = torch.cat((embedded, context_vector), 1).unsqueeze(0)
        output, hidden = self.gru(output, hidden)

        output = F.log_softmax(self.out(output[0]), dim=1)
        return output, hidden, attn_weights

    def initHidden(self):
        return torch.zeros(1, 1, self.hidden_size, device=device)


# Training
# ========
#
# Preparing Training Data
# -----------------------
#
# To train, for each pair we will need an input tensor (indexes of the
# words in the input sentence) and target tensor (indexes of the words in
# the target sentence). While creating these vectors we will append the
# EOS token to both sequences.
#

def indexesFromSentence(lang, sentence):
    #print(lang.word2index)
    newSentence = sentence;
    for tword in sentence.split(' '):
        if tword not in  lang.word2index:
            newSentence = sentence.replace(tword, ".")
            print("UNKNOWN: ", tword)
            print("Ignore following output and try another sentence")
            return []

    return [lang.word2index[word] for word in newSentence.split(' ')]


def tensorFromSentence(lang, sentence):
    indexes = indexesFromSentence(lang, sentence)
    indexes.append(EOS_token)
    return torch.tensor(indexes, dtype=torch.long, device=device).view(-1, 1)


def tensorsFromPair(pair):
    #print(pair)
    input_tensor = tensorFromSentence(input_lang, pair[0])
    target_tensor = tensorFromSentence(output_lang, pair[1])
    return (input_tensor, target_tensor)


######################################################################
# Training the Model
# ------------------
#
# To train we run the input sentence through the encoder, and keep track
# of every output and the latest hidden state. Then the decoder is given
# the ``<SOS>`` token as its first input, and the last hidden state of the
# encoder as its first hidden state.
#
# "Teacher forcing" is the concept of using the real target outputs as
# each next input, instead of using the decoder's guess as the next input.
# Using teacher forcing causes it to converge faster but `when the trained
# network is exploited, it may exhibit
# instability <http://citeseerx.ist.psu.edu/viewdoc/download?doi=10.1.1.378.4095&rep=rep1&type=pdf>`__.
#
# You can observe outputs of teacher-forced networks that read with
# coherent grammar but wander far from the correct translation -
# intuitively it has learned to represent the output grammar and can "pick
# up" the meaning once the teacher tells it the first few words, but it
# has not properly learned how to create the sentence from the translation
# in the first place.
#
# Because of the freedom PyTorch's autograd gives us, we can randomly
# choose to use teacher forcing or not with a simple if statement. Turn
# ``teacher_forcing_ratio`` up to use more of it.
#

teacher_forcing_ratio = 1


def train(input_tensor, target_tensor, encoder, decoder, encoder_optimizer, decoder_optimizer, criterion, max_length=MAX_LENGTH):
    encoder_hidden = encoder.initHidden()

    encoder_optimizer.zero_grad()
    decoder_optimizer.zero_grad()

    input_length = input_tensor.size(0)
    target_length = target_tensor.size(0)

    encoder_outputs = torch.zeros(max_length, encoder.hidden_size, device=device)

    loss = 0

    for ei in range(input_length):
        encoder_output, encoder_hidden = encoder(
            input_tensor[ei], encoder_hidden)
        encoder_outputs[ei] = encoder_output[0, 0]

    decoder_input = torch.tensor([[SOS_token]], device=device)

    decoder_hidden = encoder_hidden

    use_teacher_forcing = True if random.random() < teacher_forcing_ratio else False

    if use_teacher_forcing:
        # Teacher forcing: Feed the target as the next input
        for di in range(target_length):
            decoder_output, decoder_hidden, decoder_attention = decoder(
                decoder_input, decoder_hidden, encoder_outputs)
            #print("Using teacher forcing")
            #print(decoder_output)
            #print(target_tensor[di])
            loss += criterion(decoder_output, target_tensor[di])
            decoder_input = target_tensor[di]  # Teacher forcing

    else:
        # Without teacher forcing: use its own predictions as the next input
        for di in range(target_length):
            decoder_output, decoder_hidden, decoder_attention = decoder(
                decoder_input, decoder_hidden, encoder_outputs)
            topv, topi = decoder_output.topk(1)
            decoder_input = topi.squeeze().detach()  # detach from history as input
           
            #print("NOT Using teacher forcing")
            #print(decoder_output)
            #print(target_tensor[di])
            loss += criterion(decoder_output, target_tensor[di])

            if decoder_input.item() == EOS_token:
                break

    loss.backward()

    encoder_optimizer.step()
    decoder_optimizer.step()
    # Print this to see real time loss data
    #print(loss.item() / target_length)

    return loss.item() / target_length


######################################################################
# This is a helper function to print time elapsed and estimated time
# remaining given the current time and progress %.
#

import time
import math


def asMinutes(s):
    m = math.floor(s / 60)
    s -= m * 60
    return '%dm %ds' % (m, s)


def timeSince(since, percent):
    now = time.time()
    s = now - since
    es = s / (percent)
    rs = es - s
    return '%s (- %s)' % (asMinutes(s), asMinutes(rs))


######################################################################
# The whole training process looks like this:
#
# -  Start a timer
# -  Initialize optimizers and criterion
# -  Create set of training pairs
# -  Start empty losses array for plotting
#
# Then we call ``train`` many times and occasionally print the progress (%
# of examples, time so far, estimated time) and average loss.
#

def trainIters(encoder, decoder, n_iters, print_every=1000, plot_every=100, learning_rate=0.01):
    start = time.time()
    plot_losses = []
    print_loss_total = 0  # Reset every print_every
    plot_loss_total = 0  # Reset every plot_every

    encoder_optimizer = optim.SGD(encoder.parameters(), lr=learning_rate)
    decoder_optimizer = optim.SGD(decoder.parameters(), lr=learning_rate)
    training_pairs = [tensorsFromPair(random.choice(pairs))
                      for i in range(n_iters)
                      ]
    criterion = nn.NLLLoss()

    for iter in range(1, n_iters + 1):
        training_pair = training_pairs[iter - 1]
        input_tensor = training_pair[0]
        target_tensor = training_pair[1]

        #print("---------------")
        loss = train(input_tensor, target_tensor, encoder,
                     decoder, encoder_optimizer, decoder_optimizer, criterion)
        print_loss_total += loss
        plot_loss_total += loss

        if iter % print_every == 0:
            print_loss_avg = print_loss_total / print_every
            print_loss_total = 0
            print('%s (%d %d%%) %.4f' % (timeSince(start, iter / n_iters),
                                         iter, iter / n_iters * 100, print_loss_avg))

        if iter % plot_every == 0:
            plot_loss_avg = plot_loss_total / plot_every
            plot_losses.append(plot_loss_avg)
            plot_loss_total = 0

  #  showPlot(plot_losses)


######################################################################
# Plotting results
# ----------------
#
# Plotting is done with matplotlib, using the array of loss values
# ``plot_losses`` saved while training.
#

import matplotlib.pyplot as plt
# plt.switch_backend('agg')
import matplotlib.ticker as ticker
import numpy as np


def showPlot(points):
    plt.figure()
    fig, ax = plt.subplots()
    # this locator puts ticks at regular intervals
    loc = ticker.MultipleLocator(base=0.2)
    ax.yaxis.set_major_locator(loc)
    plt.plot(points)


######################################################################
# Evaluation
# ==========
#
# Evaluation is mostly the same as training, but there are no targets so
# we simply feed the decoder's predictions back to itself for each step.
# Every time it predicts a word we add it to the output string, and if it
# predicts the EOS token we stop there. We also store the decoder's
# attention outputs for display later.
#

def evaluate(encoder, decoder, sentence, max_length=MAX_LENGTH):
    with torch.no_grad():
        input_tensor = tensorFromSentence(input_lang, sentence)
        input_length = input_tensor.size()[0]
        encoder_hidden = encoder.initHidden()

        encoder_outputs = torch.zeros(max_length, encoder.hidden_size, device=device)

        for ei in range(input_length):
            encoder_output, encoder_hidden = encoder(input_tensor[ei],
                                                     encoder_hidden)
            encoder_outputs[ei] += encoder_output[0, 0]

        decoder_input = torch.tensor([[SOS_token]], device=device)  # SOS

        decoder_hidden = encoder_hidden

        decoded_words = []
        decoder_attentions = torch.zeros(max_length, max_length)

        for di in range(max_length):
            decoder_output, decoder_hidden, decoder_attention = decoder(
                decoder_input, decoder_hidden, encoder_outputs)
            decoder_attentions[di] = decoder_attention.data
            topv, topi = decoder_output.data.topk(1)
            if topi.item() == EOS_token:
                decoded_words.append('<EOS>')
                break
            else:
                decoded_words.append(output_lang.index2word[topi.item()])

            decoder_input = topi.squeeze().detach()

        return decoded_words, decoder_attentions[:di + 1]


######################################################################
# We can evaluate random sentences from the training set and print out the
# input, target, and output to make some subjective quality judgements:
#

def evaluateRandomly(encoder, decoder, n=20):
    tempSentence = ""
    while tempSentence != "-1":
        tempSentence = input('Enter a sentence to translate (-1 to exit): ')
        print('>', tempSentence)
        output_words, attentions = evaluate(encoder, decoder, tempSentence)
        output_sentence = ' '.join(output_words)
        print('<', output_sentence)
        print('')


######################################################################
# Training and Evaluating
# =======================
#
# With all these helper functions in place (it looks like extra work, but
# it makes it easier to run multiple experiments) we can actually
# initialize a network and start training.
#
# Remember that the input sentences were heavily filtered. For this small
# dataset we can use relatively small networks of 256 hidden nodes and a
# single GRU layer. After about 40 minutes on a MacBook CPU we'll get some
# reasonable results.
#
# .. Note::
#    If you run this notebook you can train, interrupt the kernel,
#    evaluate, and continue training later. Comment out the lines where the
#    encoder and decoder are initialized and run ``trainIters`` again.
#

hidden_size = 256
encoder = EncoderRNN(input_lang.n_words, hidden_size).to(device)
attn_decoder =  AttnDecoderRNN(hidden_size, output_lang.n_words, dropout_p=0.1).to(device)

#Change this if you want more sample sentences for system to train with. 
trainIters(encoder, attn_decoder, 2000, print_every=500)

###################################################################### 20 -25 lines.  most used words semantics? and !?, .code only knows a few words in vocab. size of encoder, decoder, attention when using partial or all docs

evaluateRandomly(encoder, attn_decoder)

#necesito saber cuándo venir.
######################################################################
# Visualizing Attention
# ---------------------
#
# A useful property of the attention mechanism is its highly interpretable
# outputs. Because it is used to weight specific encoder outputs of the
# input sequence, we can imagine looking where the network is focused most
# at each time step.
#
# You could simply run ``plt.matshow(attentions)`` to see attention output
# displayed as a matrix, with the columns being input steps and rows being
# output steps:
#

#output_words, attentions = evaluate(
#    encoder, attn_decoder, "soy un escritor .")
#plt.matshow(attentions.numpy())


######################################################################
# For a better viewing experience we will do the extra work of adding axes
# and labels:
#

def showAttention(input_sentence, output_words, attentions, tempHold):
    # Set up figure with colorbar
    fig = plt.figure()
    ax = fig.add_subplot(111)
    cax = ax.matshow(attentions.numpy(), cmap='bone')
    fig.colorbar(cax)

    # Set up axes
    ax.set_xticklabels([''] + input_sentence.split(' ') +
                       ['<EOS>'], rotation=90)
    ax.set_yticklabels([''] + output_words)

    # Show label at every tick
    ax.xaxis.set_major_locator(ticker.MultipleLocator(1))
    ax.yaxis.set_major_locator(ticker.MultipleLocator(1))

    if tempHold == 1:
        plt.savefig("mygraph1.png")
    elif tempHold == 2:
        plt.savefig("mygraph2.png")
    elif tempHold == 3:
        plt.savefig("mygraph3.png")
    elif tempHold == 4:
        plt.savefig("mygraph4.png")


def evaluateAndShowAttention(input_sentence, tempHold):
    output_words, attentions = evaluate(
        encoder, attn_decoder, input_sentence)
    print('input =', input_sentence)
    print('output =', ' '.join(output_words))
    showAttention(input_sentence, output_words, attentions, tempHold)


evaluateAndShowAttention("lo sentimos .", 1)

evaluateAndShowAttention("ella no esta aqui .", 2)

evaluateAndShowAttention("estas a dieta .", 3)

evaluateAndShowAttention("el es muy alto !", 4)

######################################################################
# Exercises
# =========
#
# -  Try with a different dataset
#
#    -  Another language pair
#    -  Human → Machine (e.g. IOT commands)
#    -  Chat → Response
#    -  Question → Answer
#
# -  Replace the embeddings with pretrained word embeddings such as ``word2vec`` or
#    ``GloVe``
# -  Try with more layers, more hidden units, and more sentences. Compare
#    the training time and results.
# -  If you use a translation file where pairs have two of the same phrase
#    (``I am test \t I am test``), you can use this as an autoencoder. Try
#    this:
#
#    -  Train as an autoencoder
#    -  Save only the Encoder network
#    -  Train a new Decoder for translation from there
#





