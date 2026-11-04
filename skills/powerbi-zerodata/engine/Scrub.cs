using System.Globalization;
using System.Text;
using System.Text.RegularExpressions;

namespace PbzEngine;

/// <summary>
/// Scrubs engine/TOM messages before they may appear in a <c>message</c> field.
///
/// Contract rules (CONTRACT.md "message fields"):
///  1. text inside '...' or [...] is kept only when it equals a known model object name, else «q»;
///  2. text inside "..." is always «q»;
///  3. remaining digit runs become «n»;
///  4. truncate to 300 characters.
/// Stricter additions (documented in README): echoed query text is removed first, and every remaining
/// unquoted word must be on a fixed allowlist (engine prose, DAX/TMDL keywords, known object names),
/// else it becomes «w». This stops unquoted data (for example ERROR() text) from passing through.
/// </summary>
public sealed class Scrubber
{
    public const int MaxLength = 300;
    const char EchoMark = '\u0001';

    readonly HashSet<string> _names = new(StringComparer.OrdinalIgnoreCase);
    readonly HashSet<string> _words = new(StringComparer.OrdinalIgnoreCase);

    static readonly string[] EngineNames = { "__pbz_c", "__pbz_l", "__pbz_r", "__pbz_v", "__pbz_x" };

    public Scrubber(IEnumerable<string> knownNames)
    {
        foreach (var w in Allowlist.Words) _words.Add(w);
        foreach (var n in EngineNames) _names.Add(n);
        foreach (var n in knownNames)
        {
            if (string.IsNullOrEmpty(n)) continue;
            _names.Add(n);
            foreach (Match m in WordRx.Matches(n)) _words.Add(m.Value);
        }
    }

    static readonly Regex WordRx = new(@"[\p{L}\p{N}_]+(?:'[\p{L}]+)*", RegexOptions.Compiled);
    static readonly Regex DigitsRx = new(@"^\p{N}+$", RegexOptions.Compiled);
    static readonly Regex CollapseW = new(@"«w»(?:[\-_.@/:]?«[wn]»)+", RegexOptions.Compiled);
    static readonly Regex CollapseN = new(@"«n»(?:[.,:]«n»)+", RegexOptions.Compiled);
    static readonly Regex Spaces = new(@"\s+", RegexOptions.Compiled);
    const string SafePunct = " .,:;!?()-/=<>+*&|%#{}~^@";

    public string Scrub(string? raw, IEnumerable<string>? echoes = null)
    {
        if (string.IsNullOrEmpty(raw)) return "";
        string s = raw;
        if (echoes != null)
            foreach (var e in echoes.Where(e => !string.IsNullOrEmpty(e) && e.Length >= 4).OrderByDescending(e => e.Length))
                s = s.Replace(e, " " + EchoMark + " ");
        var sb = new StringBuilder();
        foreach (var seg in Segments(s))
        {
            switch (seg.Kind)
            {
                case SegKind.Double:
                    sb.Append("«q»");
                    break;
                case SegKind.Single:
                    if (seg.Terminated && _names.Contains(seg.Text)) sb.Append('\'').Append(seg.Text.Replace("'", "''")).Append('\'');
                    else sb.Append("«q»");
                    break;
                case SegKind.Bracket:
                    if (seg.Terminated && _names.Contains(seg.Text)) sb.Append('[').Append(seg.Text.Replace("]", "]]")).Append(']');
                    else sb.Append("«q»");
                    break;
                default:
                    ScrubPlain(seg.Text, sb);
                    break;
            }
        }
        string outText = Spaces.Replace(sb.ToString(), " ").Trim();
        outText = CollapseW.Replace(outText, "«w»");
        outText = CollapseN.Replace(outText, "«n»");
        if (outText.Length > MaxLength)
        {
            outText = outText.Substring(0, MaxLength);
            int open = outText.LastIndexOf('«');
            if (open >= 0 && outText.IndexOf('»', open) < 0) outText = outText.Substring(0, open);
        }
        return outText.Trim();
    }

    void ScrubPlain(string text, StringBuilder sb)
    {
        int i = 0;
        while (i < text.Length)
        {
            char c = text[i];
            if (c == EchoMark) { sb.Append("«q»"); i++; continue; }
            if (char.IsLetterOrDigit(c) || c == '_')
            {
                var m = WordRx.Match(text, i);
                if (m.Success && m.Index == i && m.Length > 0)
                {
                    string w = m.Value;
                    if (DigitsRx.IsMatch(w)) sb.Append("«n»");
                    else if (_words.Contains(w)) sb.Append(w);
                    else sb.Append("«w»");
                    i += m.Length;
                    continue;
                }
                // a lone combining char or similar
                sb.Append("«w»");
                i++;
                continue;
            }
            if (char.IsWhiteSpace(c)) sb.Append(' ');
            else if (SafePunct.IndexOf(c) >= 0) sb.Append(c);
            else if (c == '\'') sb.Append('\'');
            else sb.Append(' ');
            i++;
        }
    }

    /// <summary>Message with every quoted/bracketed span blanked out (used for classification only).</summary>
    public static string StripQuoted(string message)
    {
        var sb = new StringBuilder();
        foreach (var seg in Segments(message))
            sb.Append(seg.Kind == SegKind.Plain ? seg.Text : " q ");
        return sb.ToString();
    }

    public enum SegKind { Plain, Double, Single, Bracket }

    public readonly record struct Seg(SegKind Kind, string Text, bool Terminated);

    static bool IsWordChar(char c) => char.IsLetterOrDigit(c) || c == '_';

    public static IEnumerable<Seg> Segments(string s)
    {
        var plain = new StringBuilder();
        int i = 0;
        while (i < s.Length)
        {
            char c = s[i];
            if (c == '"')
            {
                if (plain.Length > 0) { yield return new Seg(SegKind.Plain, plain.ToString(), true); plain.Clear(); }
                int j = i + 1;
                var content = new StringBuilder();
                bool term = false;
                while (j < s.Length)
                {
                    if (s[j] == '"')
                    {
                        if (j + 1 < s.Length && s[j + 1] == '"') { content.Append('"'); j += 2; continue; }
                        term = true;
                        break;
                    }
                    content.Append(s[j]);
                    j++;
                }
                yield return new Seg(SegKind.Double, content.ToString(), term);
                i = term ? j + 1 : s.Length;
                continue;
            }
            if (c == '\'' && (i == 0 || !IsWordChar(s[i - 1])))
            {
                if (plain.Length > 0) { yield return new Seg(SegKind.Plain, plain.ToString(), true); plain.Clear(); }
                int j = i + 1;
                var content = new StringBuilder();
                bool term = false;
                while (j < s.Length)
                {
                    if (s[j] == '\'')
                    {
                        if (j + 1 < s.Length && s[j + 1] == '\'') { content.Append('\''); j += 2; continue; }
                        if (j + 1 >= s.Length || !IsWordChar(s[j + 1])) { term = true; break; }
                    }
                    content.Append(s[j]);
                    j++;
                }
                yield return new Seg(SegKind.Single, content.ToString(), term);
                i = term ? j + 1 : s.Length;
                continue;
            }
            if (c == '[')
            {
                if (plain.Length > 0) { yield return new Seg(SegKind.Plain, plain.ToString(), true); plain.Clear(); }
                int j = i + 1;
                var content = new StringBuilder();
                bool term = false;
                while (j < s.Length)
                {
                    if (s[j] == ']')
                    {
                        if (j + 1 < s.Length && s[j + 1] == ']') { content.Append(']'); j += 2; continue; }
                        term = true;
                        break;
                    }
                    content.Append(s[j]);
                    j++;
                }
                yield return new Seg(SegKind.Bracket, content.ToString(), term);
                i = term ? j + 1 : s.Length;
                continue;
            }
            plain.Append(c);
            i++;
        }
        if (plain.Length > 0) yield return new Seg(SegKind.Plain, plain.ToString(), true);
    }
}

/// <summary>Words allowed to pass unquoted through a scrubbed message.</summary>
static class Allowlist
{
    public static readonly string[] Words = (
        // function words
        "a an the and or not no nor is are was were be been being it its this that these those there here of in on at to " +
        "for from by with without as into onto than then if else when while which who whom whose what where why how can " +
        "cannot could should would will may might must shall do does did done has have had having only also more less most " +
        "least one two three any all some each every other another such same both either neither but so very too again after " +
        "before during until since because due per via between among within outside inside over under above below up down " +
        "out off about against along around through across toward towards upon whether however therefore thus please try " +
        "using use used uses make sure see set get gets given give specified specify provide provided expected expects " +
        "expect found find contain contains containing contained include includes including refer refers referring referenced " +
        "reference references like unlike instead rather well just still yet already now currently consider ensure check " +
        "can't cannot couldn't doesn't don't didn't isn't aren't wasn't weren't hasn't haven't hadn't won't wouldn't shouldn't " +
        "it's that's there's let's " +
        // engine prose
        "error errors warning warnings failed failure fail fails invalid valid unknown unexpected missing incorrect correct " +
        "wrong unsupported supported support supports allowed allow allows permitted required requires require ambiguous " +
        "duplicate duplicates circular dependency dependencies detected detect occurred occur occurs exception problem cause " +
        "caused resolve resolved resolution exist exists existing determined determine single multiple many few value values " +
        "type types text number numbers integer whole decimal double currency fixed boolean logical true false date datetime " +
        "time string binary variant blank blanks empty null convert converted conversion cast compare comparing comparison " +
        "comparisons operation operations operator operators operand operands argument arguments parameter parameters function " +
        "functions expression expressions formula formulas query queries calculation calculations calculated calculate measure " +
        "measures column columns table tables row rows current context filter filters filtered relationship relationships " +
        "hierarchy hierarchies level levels model database partition partitions role roles culture cultures perspective " +
        "perspectives object objects property properties name names named variable variables syntax token tokens line lines " +
        "document documents indentation parsing parse parsed format formatted formats character characters position keyword " +
        "keywords identifier identifiers quote quotes quoted bracket brackets parenthesis parentheses comma commas end ends " +
        "start starts begin beginning input output result results return returns returned scalar list element elements " +
        "aggregation aggregations aggregate min max count sum average minimum maximum total totals data source sources key " +
        "keys unique primary foreign side sides cardinality direction active inactive defined define definition definitions " +
        "declared declare mode import directquery dual server engine memory timed cancelled canceled connection session request " +
        "command commands processing process processed refresh refreshed state ready needed need needs recalculation recalc " +
        "semantic evaluation evaluate evaluated evaluating statement statements path paths serializing serialization " +
        "deserializing deserialization length size large larger small smaller greater equal equals different differs match " +
        "matches matching mismatch overflow division divide zero negative positive infinity numeric digit digits letter letters " +
        "case sensitive insensitive whitespace space tab newline known undefined available unavailable visible hidden private " +
        "public internal external system user users member members permission permissions security access denied granted read " +
        "write restriction restricted limit limits exceeded exceed version compatibility feature features enabled disabled option " +
        "options setting settings automatic manual explicit implicit inferred default new old original previous next first last " +
        "second top bottom left right inner outer full cross join union intersect except group grouping order sort sorted " +
        "ascending descending rank window offset index lookup related separator delimiter pattern step steps applied apply " +
        "changed change changes rename renamed added add removed remove deleted delete created create modified modify updated " +
        "update saved save loaded load opened open closed close specified contains multiple determined single calculation " +
        "occurred encountered encountering instance instances item items attribute attributes member dimension cube level " +
        "visual visuals report page pages field fields entity entities selection selected select unrelated related many-to-many " +
        "detail details detailed message messages reason reasons operation exceeded resources resource capacity available " +
        "variation variations alternate attempt attempted attempting provided supplied received receive valid " +
        "happen happens happened happening specifying specifies specify result resulting results get got getting " +
        "able unable possible impossible allowed only following follow follows preceding precede within without " +
        "expected expecting expects requires required instead whereas although though unless otherwise also " +
        "string strings numeric texts dates times values columns rows tables measures functions arguments " +
        "returned returning returns refers referring referred currently currently specified possibly probably " +
        "maybe likely unlikely reached reach reaches exceeds exceeding modify modifying modifies apply applying " +
        "applies applied enough insufficient sufficient total totals subtotal subtotals " +
        // DAX keywords and functions that appear unquoted in engine messages
        "var return define evaluate measure asc desc order by start at in not and or true false blank " +
        "abs addcolumns addmissingitems all allcrossfiltered allexcept allnoblankrow allselected average averagea averagex " +
        "calculate calculatetable calendar calendarauto ceiling coalesce combinevalues concatenate concatenatex contains " +
        "containsrow containsstring containsstringexact count counta countax countblank countrows countx crossfilter crossjoin " +
        "currentgroup datatable dateadd datediff datesbetween datesinperiod datesmtd datesqtd datesytd datevalue day detailrows " +
        "distinct distinctcount distinctcountnoblank divide earlier earliest edate eomonth evaluateandlog exp filters find " +
        "firstdate firstnonblank firstnonblankvalue floor generate generateall generateseries groupby hasonefilter hasonevalue " +
        "hour iferror ignore int isblank iscrossfiltered isempty iserror iseven isfiltered isinscope islogical isnontext " +
        "isnumber isodd isonorafter isselectedmeasure issubtotal istext keepfilters lastdate lastnonblank lastnonblankvalue len " +
        "ln log lookupvalue lower maxa maxx median medianx mid mina minx minute mod month naturalinnerjoin " +
        "naturalleftouterjoin nextday nextmonth nextquarter nextyear nonvisual now orderby parallelperiod partitionby " +
        "pathcontains pathitem pathlength percentile power previousday previousmonth previousquarter previousyear quarter rand " +
        "randbetween rankx relatedtable removefilters replace rept rollup rollupaddissubtotal round rounddown roundup rownumber " +
        "sameperiodlastyear sample search selectcolumns selectedmeasure selectedmeasurename selectedvalue sign sqrt " +
        "startofmonth startofquarter startofyear substitute substitutewithindex summarize summarizecolumns sumx switch " +
        "timevalue today topn totalmtd totalqtd totalytd treatas trim trunc unichar unicode upper userelationship username " +
        "userobjectid userprincipalname weekday weeknum xirr xnpv year yearfrac if row error " +
        // TMDL / TOM vocabulary
        "tmdl tom amo xmla tmsl json dax mdx indentation invalidlinetype invalidvalueformat unknownkeyword invalidproperty " +
        "invalidpropertyvalue unexpectedline linetype valueformat datatype lineagetag sourcecolumn formatstring summarizeby " +
        "annotation annotations ishidden displayfolder sortbycolumn datacategory iskey isnameinferred description querygroup " +
        "calculationgroup calculationitem compatibilitylevel modelpermission tablepermission fromcolumn tocolumn " +
        "crossfilteringbehavior isactive ref cultureinfo expressions datasource datasources int64 detected was were " +
        "relationship tablepermissions ordinal precedence"
    ).Split(' ', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
}
